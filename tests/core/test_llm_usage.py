import json
import logging
import re
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from app.core import llm_usage as U
from app.core.llm import INVALID_MODEL_NAME, MAX_TOKEN_COUNT

"""
    모델 사용량 합계·예상 비용·사용량 로그 (app/core/llm_usage.py, NFR-008)
      모델을 부르지 않는다. 호출별 사용량(숫자)을 넣어 합계와 금액, 로그 줄만 본다.
      1. 합계: 호출 수·실패 수·토큰 합. 요청이 나가지 않은 시도는 세지 않고, 토큰 수를 모르는 호출은 따로 센다
      2. 예상 비용: 표의 단가로 손계산한 값과 같다(캐시에서 읽은 입력은 할인 단가). 표에 없는 모델은 금액이 없다
      3. mock: 금액 0·출처 mock, 모델 호출 라이브러리를 불러오지 않는다
      4. 믿을 수 없는 값: 음수·터무니없이 큰 수·캐시 > 입력은 합계에 넣지 않는다. 금액이 음수가 되지 않는다
      5. 기록에 남는 값은 정수·참거짓·None·허용한 글자의 문자열뿐이다(자유 문장·줄바꿈 없음)
      6. 사용량 로그: 표준 출력에 JSON 한 줄. 서버 기본 로깅 설정에서도 남고, 값에 줄바꿈이 있어도 한 줄이다
"""

PRICED = (
    "openai/gpt-5.4-mini"  # 입력 0.75 · 캐시 입력 0.075 · 출력 4.50 (USD / 100만 토큰)
)
UNPRICED = "openai/model-not-in-table"
MOCK = "mock/mock-template-v1"
SAFE_TEXT = re.compile(r"[A-Za-z0-9._:/-]{1,80}")
FORGED = "gpt-x\n2026-11-12 00:00:00 INFO forged line"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _ok(input_tokens, output_tokens, cached=None, **extra):
    call = {"input_tokens": input_tokens, "output_tokens": output_tokens, **extra}
    if cached is not None:
        call["cached_input_tokens"] = cached
    return call


# --- 1. 합계 ----------------------------------------------------------------------------------


def test_summary_counts_calls_and_adds_tokens():
    calls = [
        {"error": "TIMEOUT", "elapsed_ms": 12000},  # 응답을 못 받음 → 토큰 수 모름
        _ok(2000, 300, cached=1536, error="TRUNCATED"),  # 잘린 응답: 실패지만 청구됨
        _ok(2100, 480, response_model="gpt-5.4-mini-2026-03-17"),
    ]

    summary = U.summarize_usage(PRICED, calls)

    assert summary["model"] == PRICED
    assert summary["response_models"] == ["gpt-5.4-mini-2026-03-17"]
    assert (summary["calls"], summary["calls_failed"]) == (3, 2)
    assert summary["calls_without_usage"] == 1
    assert summary["input_tokens"] == 4100
    assert summary["cached_input_tokens"] == 1536
    assert summary["output_tokens"] == 780


def test_attempt_that_never_reached_the_provider_is_not_a_call():
    calls = [
        {"error": "TIMEOUT", "elapsed_ms": 20000},
        {"error": "BUDGET_EXHAUSTED", "elapsed_ms": 0, "requested": False},
        {"error": "BUDGET_EXHAUSTED", "elapsed_ms": 0, "requested": False},
    ]

    summary = U.summarize_usage(PRICED, calls)

    assert (summary["calls"], summary["calls_failed"]) == (1, 1)
    assert summary["calls_without_usage"] == 1


def test_tokens_are_none_when_no_call_reported_usage():
    summary = U.summarize_usage(PRICED, [{"error": "TIMEOUT"}])

    # 0 이 아니라 None: 0 은 "썼는데 0"으로 읽힌다
    assert summary["input_tokens"] is None
    assert summary["cached_input_tokens"] is None
    assert summary["output_tokens"] is None


# --- 2. 예상 비용 ------------------------------------------------------------------------------


def test_cost_uses_cached_price_for_cached_input():
    summary = U.summarize_usage(PRICED, [_ok(2000, 300, cached=1536)])

    # (2000-1536)*0.75 + 1536*0.075 + 300*4.50 = 348 + 115.2 + 1350 = 1813.2 → / 1,000,000
    assert summary["estimated_cost_usd"] == "0.001813"
    assert summary["cost_complete"] is True
    assert summary["price"] == {
        "source": U.PRICE_SOURCE,
        "input_per_1m_usd": "0.75",
        "cached_input_per_1m_usd": "0.075",
        "output_per_1m_usd": "4.50",
    }


def test_cost_without_cache_charges_full_input_price():
    summary = U.summarize_usage(PRICED, [_ok(2000, 300)])

    # 2000*0.75 + 300*4.50 = 2850 → 0.002850 (캐시 단가로 계산했다면 0.001500)
    assert summary["estimated_cost_usd"] == "0.002850"


def test_cost_is_marked_incomplete_when_a_call_has_no_usage():
    summary = U.summarize_usage(PRICED, [{"error": "TIMEOUT"}, _ok(1000, 100)])

    assert summary["estimated_cost_usd"] == "0.001200"  # 아는 호출만: 750 + 450
    assert summary["cost_complete"] is False


def test_model_not_in_price_table_has_no_cost():
    summary = U.summarize_usage(UNPRICED, [_ok(1000, 100)])

    assert summary["estimated_cost_usd"] is None
    assert summary["cost_complete"] is False
    assert summary["price"] == {
        "source": U.PRICE_SOURCE_UNKNOWN,
        "input_per_1m_usd": None,
        "cached_input_per_1m_usd": None,
        "output_per_1m_usd": None,
    }
    assert summary["input_tokens"] == 1000  # 토큰 수는 그대로 남는다


def test_price_table_is_sane():
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", U.PRICE_AS_OF)
    assert U.PRICE_SOURCE.endswith(U.PRICE_AS_OF)
    assert U.PRICE_SOURCE_URL.startswith("https://")
    for name, price in U.MODEL_PRICES.items():
        assert SAFE_TEXT.fullmatch(name) and name.startswith("openai/")
        assert Decimal(0) < price.cached_input_per_1m_usd <= price.input_per_1m_usd
        assert price.output_per_1m_usd > Decimal(0)
    with pytest.raises(TypeError):  # 실행 중에 단가를 바꿔 넣을 수 없다
        U.MODEL_PRICES["openai/new"] = U.MODEL_PRICES[PRICED]  # type: ignore[index]


# --- 3. mock ----------------------------------------------------------------------------------


def test_mock_records_calls_with_zero_cost():
    summary = U.summarize_usage(
        MOCK, [{}, {}]
    )  # 사용량을 모으지 않는 어댑터: 시도 수만

    assert summary["model"] == MOCK
    assert (summary["calls"], summary["calls_failed"]) == (2, 0)
    assert summary["input_tokens"] is None
    assert summary["estimated_cost_usd"] == "0.000000"
    assert summary["cost_complete"] is True
    assert summary["price"]["source"] == U.PRICE_SOURCE_MOCK


def test_usage_module_does_not_load_the_model_library():
    """합계·비용·로그까지 돌려도 모델 호출 라이브러리가 메모리에 올라오지 않는다(mock 서버는 가볍게)."""
    code = (
        "import sys\n"
        "from app.core import llm_usage as U\n"
        "s = U.summarize_usage('openai/gpt-5.4-mini', [{'input_tokens': 10, 'output_tokens': 5}])\n"
        "U.log_model_usage('explain', U.summarize_usage('mock/mock-template-v1', [{}]))\n"
        "print('LOADED', sorted(m for m in sys.modules if m.split('.')[0] in ('litellm', 'openai')))\n"
        "print('COST', s['estimated_cost_usd'])\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=PROJECT_ROOT,
        timeout=120,
    )

    assert done.returncode == 0, done.stderr[-2000:]
    assert "LOADED []" in done.stdout
    assert "COST 0.000030" in done.stdout


# --- 4. 믿을 수 없는 값 -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        _ok(-5, 100),
        _ok(100, -1),
        _ok(MAX_TOKEN_COUNT + 1, 100),
        _ok(10**30, 100),
        _ok(100, 50, cached=101),  # 캐시 > 입력
        _ok(100, 50, cached=-1),
        _ok(True, 50),
        _ok("100", 50),
        _ok(1.5, 50),
        _ok(None, 50),
        _ok(100, None),
    ],
)
def test_untrusted_token_counts_are_left_out(call):
    summary = U.summarize_usage(PRICED, [call, _ok(1000, 100)])

    assert summary["calls"] == 2
    assert summary["calls_without_usage"] == 1
    assert (summary["input_tokens"], summary["output_tokens"]) == (1000, 100)
    assert summary["estimated_cost_usd"] == "0.001200"  # 믿을 수 있는 호출만
    assert summary["cost_complete"] is False
    assert Decimal(summary["estimated_cost_usd"]) >= 0


# --- 5. 기록에 남는 값의 종류 -----------------------------------------------------------------


def _values(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _values(item)
    else:
        yield value


@pytest.mark.parametrize("model", [PRICED, UNPRICED, MOCK, FORGED])
def test_summary_holds_only_numbers_and_safe_text(model):
    calls = [
        _ok(2000, 300, cached=10, response_model=FORGED, note="free text 자유 문장"),
        {"error": "B-ORD-20260101-0040 주문 코드가 든 사유", "prompt": "PROMPT 원문"},
    ]

    summary = U.summarize_usage(model, calls)

    for value in _values(summary):
        assert (
            value is None
            or isinstance(value, bool | int)
            or (isinstance(value, str) and SAFE_TEXT.fullmatch(value))
        ), value
    dumped = json.dumps(summary, ensure_ascii=False)
    for leaked in ("forged", "자유 문장", "B-ORD", "PROMPT", "\\n"):
        assert leaked not in dumped
    assert summary["response_models"] == [INVALID_MODEL_NAME]
    if model == FORGED:
        assert summary["model"] == INVALID_MODEL_NAME


# --- 6. 사용량 로그 ---------------------------------------------------------------------------


def test_usage_log_is_one_json_line_on_stdout(capsys):
    summary = U.summarize_usage(PRICED, [_ok(2000, 300, response_model=FORGED)])

    U.log_model_usage("explain", summary, analysis_run_id="RUN-1\nforged run line")

    out = capsys.readouterr().out
    lines = out.splitlines()
    assert len(lines) == 1  # 값에 줄바꿈이 있어도 가짜 줄이 생기지 않는다
    record = json.loads(lines[0])
    assert record["event"] == "model_usage"
    assert record["feature"] == "explain"
    assert record["analysis_run_id"] == INVALID_MODEL_NAME
    assert record["estimated_cost_usd"] == "0.002850"
    assert record["response_models"] == [INVALID_MODEL_NAME]
    assert "forged" not in out


def test_usage_logger_gets_one_handler_and_does_not_propagate(capsys):
    for _ in range(3):
        logger = U.usage_logger()
    U.log_model_usage("explain", U.summarize_usage(MOCK, [{}]))

    # 시험 도구(pytest)가 붙이는 수집용 출력은 빼고 센다
    assert sum(isinstance(h, U._StdoutHandler) for h in logger.handlers) == 1
    assert logger.propagate is False
    assert (
        len(capsys.readouterr().out.splitlines()) == 1
    )  # 같은 줄이 두 번 나오지 않는다
    # 앱 전체 로깅 수준은 그대로다(앱 로거의 INFO 는 여전히 꺼져 있음)
    assert not logging.getLogger("app.core.llm").isEnabledFor(logging.INFO)


def test_usage_line_survives_server_default_logging():
    """서버(uvicorn) 기본 로깅 설정에서 앱 로거의 INFO 줄은 버려진다. 사용량 줄은 전용 출력이라 남는다."""
    code = (
        "import logging, logging.config\n"
        "from uvicorn.config import LOGGING_CONFIG\n"
        "logging.config.dictConfig(LOGGING_CONFIG)\n"
        "from app.core import llm_usage as U\n"
        "logging.getLogger('app.core.llm').info('APP-INFO-LINE')\n"
        "U.log_model_usage('explain', U.summarize_usage('mock/mock-template-v1', [{}]), analysis_run_id='run-1')\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=PROJECT_ROOT,
        timeout=120,
    )

    assert done.returncode == 0, done.stderr[-2000:]
    assert "APP-INFO-LINE" not in done.stdout + done.stderr
    lines = done.stdout.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["analysis_run_id"] == "run-1"
