import json
import logging
import sys
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from types import MappingProxyType
from typing import Any

from app.core.llm import MAX_TOKEN_COUNT, safe_model_name

"""
    모델 사용량 합계와 예상 비용 (NFR-008 "모델 사용 비용 기록", docs/agent-flow/explanation.md 8절·9절)
      - 모델을 부른 기능이 호출별 사용량을 넘기면 합계(호출 수·토큰 수)와 예상 비용을 만든다. 숫자와 정해진 코드만 다룬다.
      - 단가는 이 파일의 표에서만 읽는다. 실행 중에 밖에서 받아오지 않고, 모델 호출 라이브러리의 가격 데이터도 쓰지 않는다.
        단가를 바꾸면 변경 내용에 숫자가 그대로 보인다. 표를 고칠 때는 PRICE_AS_OF 도 함께 고친다.
      - 기록마다 그때 쓴 단가와 출처를 함께 적는다. 나중에 표가 바뀌어도 옛 기록을 그 기록만으로 읽을 수 있다.
      - 예상 비용은 정가 기준 추정이다. 요금 등급·할인, 응답을 받지 못한 호출(시간 초과 등)의 청구는 반영하지 못한다.
        토큰 수를 모르는 호출이 있으면 cost_complete 가 false 다(실제보다 적을 수 있음).
      - 이 파일은 모델 호출 라이브러리를 불러오지 않는다. mock 만 쓰는 서버도 그대로 가볍다.
      - 사용량 로그: 전용 로거가 표준 출력에 JSON 한 줄을 쓴다. 앱 로거는 기본 수준이 WARNING 이라 INFO 줄이 남지 않고,
        문장으로 쓰면 값에 든 줄바꿈으로 가짜 줄이 만들어질 수 있어서다. 앱 전체 로깅 설정은 건드리지 않는다.
"""

# 단가 출처: OpenAI 공식 가격 페이지의 Standard 등급 글 토큰 단가 (USD / 100만 토큰)
PRICE_SOURCE_URL = "https://developers.openai.com/api/docs/pricing"
PRICE_AS_OF = "2026-11-12"  # 위 페이지에서 확인한 날
PRICE_SOURCE = f"openai-standard-{PRICE_AS_OF}"  # 기록에 적는 단가 출처
PRICE_SOURCE_MOCK = "mock"  # 실제 호출이 없는 결정적 mock (비용 0)
PRICE_SOURCE_UNKNOWN = "unknown"  # 표에 없는 모델 (금액을 만들지 않음)

MOCK_PROVIDER = "mock"
USAGE_LOGGER_NAME = "app.model_usage"

_MILLION = Decimal(1_000_000)
_CENT_MILLIONTH = Decimal("0.000001")  # 금액은 USD 소수 6자리
_MAX_RESPONSE_MODELS = 5


@dataclass(frozen=True)
class ModelPrice:
    """모델 하나의 단가 (USD / 100만 토큰)"""

    input_per_1m_usd: Decimal
    cached_input_per_1m_usd: Decimal
    output_per_1m_usd: Decimal


def _price(input_usd: str, cached_input_usd: str, output_usd: str) -> ModelPrice:
    return ModelPrice(
        Decimal(input_usd), Decimal(cached_input_usd), Decimal(output_usd)
    )


# "제공자/모델 ID" → 단가. 설정에 넣는 모델 이름과 글자 그대로 같아야 한다(날짜가 붙은 판 이름은 따로 넣는다)
MODEL_PRICES: Mapping[str, ModelPrice] = MappingProxyType(
    {
        "openai/gpt-5.4-mini": _price("0.75", "0.075", "4.50"),
        "openai/gpt-5.4-nano": _price("0.20", "0.02", "1.25"),
        "openai/gpt-5-mini": _price("0.25", "0.025", "2.00"),
        "openai/gpt-4.1-mini": _price("0.40", "0.10", "1.60"),
    }
)


def summarize_usage(model: str, calls: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """
    호출별 사용량 → 합계와 예상 비용. model 은 요청한 "제공자/모델 ID".
    호출 하나는 아래 칸을 가진 사전이다(없는 칸은 None 으로 본다).
      input_tokens · output_tokens · cached_input_tokens : 제공자가 준 토큰 수
      response_model : 제공자가 답한 모델 이름
      error          : 실패 사유 코드(성공이면 없음)
      requested      : False 면 제공자에게 요청이 나가지 않은 시도(시간 예산 소진 등) — 호출 수에 넣지 않는다
    돌려주는 값은 정수·참거짓·None·허용한 글자의 문자열뿐이다(자유 문장 없음).
    """
    count = failed = without_usage = 0
    input_tokens = cached_tokens = output_tokens = 0
    measured = 0
    response_models: set[str] = set()
    for call in calls:
        if call.get("requested") is False:
            continue
        count += 1
        if call.get("error"):
            failed += 1
        answered = call.get("response_model")
        if answered:
            response_models.add(safe_model_name(answered))
        tokens = _tokens(call)
        if tokens is None:
            without_usage += 1
            continue
        measured += 1
        input_tokens += tokens[0]
        cached_tokens += tokens[1]
        output_tokens += tokens[2]

    summary: dict[str, Any] = {
        "model": safe_model_name(model),
        "response_models": sorted(response_models)[:_MAX_RESPONSE_MODELS],
        "calls": count,
        "calls_failed": failed,
        "calls_without_usage": without_usage,
        # 사용량을 아는 호출이 하나도 없으면 0 이 아니라 None 이다(0 은 "썼는데 0"으로 읽힌다)
        "input_tokens": input_tokens if measured else None,
        "cached_input_tokens": cached_tokens if measured else None,
        "output_tokens": output_tokens if measured else None,
    }
    summary.update(
        _cost(model, input_tokens, cached_tokens, output_tokens, without_usage)
    )
    return summary


def _tokens(call: Mapping[str, Any]) -> tuple[int, int, int] | None:
    """(입력, 캐시에서 읽은 입력, 출력). 하나라도 믿을 수 없으면 None(공용 호출 층과 같은 기준으로 한 번 더 본다)."""
    input_tokens = call.get("input_tokens")
    output_tokens = call.get("output_tokens")
    cached = call.get("cached_input_tokens")
    if not (_is_count(input_tokens) and _is_count(output_tokens)):
        return None
    if cached is None:
        return input_tokens, 0, output_tokens
    if not _is_count(cached) or cached > input_tokens:
        return None
    return input_tokens, cached, output_tokens


def _is_count(value: Any) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= MAX_TOKEN_COUNT
    )


def _cost(
    model: str,
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    without_usage: int,
) -> dict[str, Any]:
    if model.partition("/")[0] == MOCK_PROVIDER:
        return _cost_fields(Decimal(0), complete=True, source=PRICE_SOURCE_MOCK)
    price = MODEL_PRICES.get(model)
    if price is None:
        return _cost_fields(None, complete=False, source=PRICE_SOURCE_UNKNOWN)
    amount = (
        (input_tokens - cached_tokens) * price.input_per_1m_usd
        + cached_tokens * price.cached_input_per_1m_usd
        + output_tokens * price.output_per_1m_usd
    ) / _MILLION
    return _cost_fields(
        amount, complete=without_usage == 0, source=PRICE_SOURCE, price=price
    )


def _cost_fields(
    amount: Decimal | None,
    *,
    complete: bool,
    source: str,
    price: ModelPrice | None = None,
) -> dict[str, Any]:
    return {
        "estimated_cost_usd": None if amount is None else _usd(amount),
        "cost_complete": complete,
        "price": {
            "source": source,
            "input_per_1m_usd": str(price.input_per_1m_usd) if price else None,
            "cached_input_per_1m_usd": (
                str(price.cached_input_per_1m_usd) if price else None
            ),
            "output_per_1m_usd": str(price.output_per_1m_usd) if price else None,
        },
    }


def _usd(amount: Decimal) -> str:
    return format(amount.quantize(_CENT_MILLIONTH, rounding=ROUND_HALF_UP), "f")


# --- 사용량 로그 (표준 출력, JSON 한 줄) ---------------------------------------------------------


class _StdoutHandler(logging.StreamHandler):
    """항상 지금의 sys.stdout 에 쓴다(서버가 뜬 뒤 표준 출력이 바뀌어도 따라간다)."""

    def __init__(self) -> None:
        logging.Handler.__init__(self)

    @property
    def stream(self):
        return sys.stdout


_logger_lock = threading.Lock()


def usage_logger() -> logging.Logger:
    """모델 사용량 전용 로거. 출력은 한 번만 붙이고, 다른 로거로 올려 보내지 않는다(같은 줄이 두 번 나오지 않게)."""
    logger = logging.getLogger(USAGE_LOGGER_NAME)
    if not any(isinstance(handler, _StdoutHandler) for handler in logger.handlers):
        with _logger_lock:
            if not any(
                isinstance(handler, _StdoutHandler) for handler in logger.handlers
            ):
                handler = _StdoutHandler()
                handler.setFormatter(logging.Formatter("%(message)s"))
                logger.addHandler(handler)
                logger.setLevel(logging.INFO)
                logger.propagate = False
    return logger


def log_model_usage(
    feature: str, summary: Mapping[str, Any], *, analysis_run_id: str | None = None
) -> None:
    """
    사용량 합계를 서버 로그에 JSON 한 줄로 남긴다. feature 는 모델을 부른 기능 이름(예: "explain").
    분석 실행 안에서 부른 것이면 analysis_run_id 를 넘긴다(실행별 기록). 고객사 ID·업무 값은 넣지 않는다.
    """
    record = {
        "event": "model_usage",
        "feature": safe_model_name(feature),
        "analysis_run_id": (
            safe_model_name(analysis_run_id) if analysis_run_id is not None else None
        ),
        **summary,
    }
    usage_logger().info(json.dumps(record, ensure_ascii=False, sort_keys=True))
