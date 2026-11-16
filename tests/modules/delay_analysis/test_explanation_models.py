import asyncio
import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr, ValidationError

from app import main as app_main
from app.core import llm_usage as U
from app.core.config import MOCK_EXPLANATION_MODEL, Settings, get_settings
from app.core.llm import (
    REASON_RATE_LIMIT,
    REASON_TIMEOUT,
    REASON_TRUNCATED,
    LlmCallError,
    LlmResult,
    LlmTimeoutError,
    LlmUsage,
)
from app.main import app, lifespan
from app.modules.delay_analysis.application.agent import explanation as X
from app.modules.delay_analysis.infrastructure.engine import explanation_assets as XA
from app.modules.delay_analysis.infrastructure.engine.engine_loader import (
    load_engine_module,
)
from app.modules.delay_analysis.infrastructure.explanation_models import adapter as A
from app.modules.delay_analysis.infrastructure.explanation_models import (
    selection as S,
)
from tests.modules.delay_analysis.engine_test_modules import (
    load_engine_test_module,
)

"""
    AI 설명 실제 모델 연결 준비 (GLOW-150, docs/agent-flow/explanation.md 8절)
      외부로 나가지 않는다. 모델 호출은 가짜 함수로 바꾼다(공용 호출 층 자체는 tests/core/test_llm.py 가 본다).
      1. 설정: 기본은 mock, "제공자/모델 ID" 형식, 빈 키는 없는 것
      2. 어댑터(엔진 없이): 호출 인자, 오류 변환, 총 시간 예산, 시도별 사용량
      3. 고르기·서버 시작 확인(엔진 없이): mock 은 아무것도 안 함, 실제 모델은 엔진 경로·제공자·키가 있어야 함
      4. 엔진과 함께: 등록된 모델이면 엔진 3시도 경로가 통과·실패·예산 소진으로 이어지고, 등록 안 된 모델은 호출 전에 멈춤
      5. 설명 한 건마다 새 어댑터(동시 실행이 예산·사용량을 섞지 않음), mock 서버는 모델 호출 라이브러리를 불러오지 않음
      6. 모델 사용량 기록(NFR-008): 설명 한 건의 모델명·호출 수·토큰 수·예상 비용이 추적 사건 하나와 서버 로그 한 줄로 남는다.
         mock 도 남고, 기록이 망가져도 설명 결과는 같으며, 저장되는 설명에는 사용량이 섞이지 않는다
"""

API_KEY = "sk-test-KEEP-THIS-SECRET-0123456789"
REQUIRED = {
    "app_env": "local",
    "postgres_host": "db",
    "postgres_port": 5432,
    "postgres_user": "app_login",
    "postgres_password": "app_pw",
    "postgres_db": "glowship",
    "jwt_secret_key": "x" * 32,
}
LLM_ENV = (
    "EXPLANATION_MODEL",
    "EXPLANATION_MODEL_TIMEOUT_SECONDS",
    "EXPLANATION_TOTAL_BUDGET_SECONDS",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "ANTHROPIC_API_KEY",
)
RUN_ID = "11111111-2222-3333-4444-555555555555"
NOW = datetime(2026, 3, 10, 0, 5, tzinfo=UTC)
SHIP = "SHIP-DELAYED"
TEST_MODEL_ID = "test-model-1"
TEST_MODEL = f"openai/{TEST_MODEL_ID}"


@pytest.fixture(autouse=True)
def no_llm_env(monkeypatch):
    """이 PC·CI 의 환경 변수가 설정 시험에 섞이지 않게 한다."""
    for name in LLM_ENV:
        monkeypatch.delenv(name, raising=False)


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **(REQUIRED | overrides))


def _real_settings(model=TEST_MODEL, **overrides) -> Settings:
    values = {
        "glowship_engine_dir": get_settings().glowship_engine_dir,
        "explanation_model": model,
        "openai_api_key": API_KEY,
    }
    return _settings(**(values | overrides))


# --- 1. 설정 ---------------------------------------------------------------------------------


def test_default_model_is_engine_mock():
    settings = _settings()

    assert (
        settings.explanation_model == MOCK_EXPLANATION_MODEL == "mock/mock-template-v1"
    )
    assert S.uses_mock_model(settings) is True
    assert settings.openai_api_key is None
    assert settings.explanation_model_timeout_seconds == 12.0
    assert settings.explanation_total_budget_seconds == 20.0


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_model_means_mock(value):
    assert (
        _settings(explanation_model=value).explanation_model == MOCK_EXPLANATION_MODEL
    )


@pytest.mark.parametrize("value", ["openai", "/gpt-x", "openai/"])
def test_model_needs_provider_and_model_id(value):
    with pytest.raises(ValidationError, match="제공자/모델 ID"):
        _settings(explanation_model=value)


def test_model_and_keys_come_from_environment(monkeypatch):
    monkeypatch.setenv("EXPLANATION_MODEL", " openai/gpt-x ")
    monkeypatch.setenv("OPENAI_API_KEY", API_KEY)
    monkeypatch.setenv("GEMINI_API_KEY", "  ")

    settings = _settings()

    assert settings.explanation_model == "openai/gpt-x"
    assert settings.openai_api_key.get_secret_value() == API_KEY
    assert settings.gemini_api_key is None  # 빈 키는 없는 것
    assert API_KEY not in repr(settings)
    assert API_KEY not in str(settings.openai_api_key)


@pytest.mark.parametrize(
    "name", ["explanation_model_timeout_seconds", "explanation_total_budget_seconds"]
)
def test_time_limits_must_be_positive(name):
    with pytest.raises(ValidationError):
        _settings(**{name: 0})


# --- 2. 어댑터 (엔진 없이) --------------------------------------------------------------------


class _EngineTimeout(Exception):
    pass


class _EngineCallError(Exception):
    pass


@pytest.fixture
def engine_errors(monkeypatch):
    """엔진 explain.adapter 의 두 예외 자리(엔진 없이 어댑터만 볼 때)"""
    errors = SimpleNamespace(
        ModelTimeout=_EngineTimeout, ModelCallError=_EngineCallError
    )

    def load(name):
        assert name == "explain.adapter"
        return errors

    monkeypatch.setattr(A, "load_engine_module", load)
    return errors


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeLlm:
    """공용 호출 층 자리. script 의 항목을 차례로 쓴다: 글(성공) 또는 예외. took 초만큼 시계를 돌린다."""

    def __init__(self, script, clock=None, took=0.0):
        self.script = list(script)
        self.clock = clock
        self.took = took
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.clock is not None:
            self.clock.now += self.took
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return LlmResult(
            text=step,
            model=kwargs["model"],
            response_model="answered-model",
            usage=LlmUsage(input_tokens=1200, output_tokens=900),
            elapsed_ms=int(self.took * 1000),
        )


def _adapter(llm, clock=None, call_timeout=12.0, budget=20.0):
    return A.ExplanationModelAdapter(
        provider="openai",
        model_id="gpt-x",
        api_key=SecretStr(API_KEY),
        call_timeout_seconds=call_timeout,
        total_budget_seconds=budget,
        complete=llm,
        clock=clock or Clock(),
    )


REQUEST = {"input_document": {"secret": "doc"}, "canary": "CNRY-abc", "attempt": 1}


def test_adapter_calls_model_with_prompt_only(engine_errors):
    llm = FakeLlm(['{"ok": true}'])
    adapter = _adapter(llm)

    assert adapter.complete("PROMPT", REQUEST) == '{"ok": true}'

    assert (adapter.provider, adapter.model_id) == ("openai", "gpt-x")
    (call,) = llm.calls
    assert set(call) == {
        "model",
        "prompt",
        "api_key",
        "timeout_seconds",
        "response_format",
    }
    assert call["model"] == "openai/gpt-x"
    assert call["prompt"] == "PROMPT"
    assert call["api_key"].get_secret_value() == API_KEY
    assert call["timeout_seconds"] == 12.0
    assert call["response_format"] == {"type": "json_object"}
    # 입력 문서·canary 가 든 request 는 넘기지 않는다
    assert "CNRY-abc" not in repr(call)
    assert adapter.attempt_usage == [
        {
            "input_tokens": 1200,
            "output_tokens": 900,
            "elapsed_ms": 0,
            "response_model": "answered-model",
        }
    ]


@pytest.mark.parametrize(
    ("llm_error", "engine_error"),
    [
        (LlmTimeoutError(REASON_TIMEOUT, model="openai/gpt-x"), _EngineTimeout),
        (
            LlmCallError(REASON_RATE_LIMIT, model="openai/gpt-x", status_code=429),
            _EngineCallError,
        ),
    ],
)
def test_adapter_turns_call_failures_into_engine_errors(
    engine_errors, llm_error, engine_error
):
    clock = Clock()
    adapter = _adapter(FakeLlm([llm_error], clock, took=3.0), clock)

    with pytest.raises(engine_error) as caught:
        adapter.complete("PROMPT", REQUEST)

    assert str(caught.value) == llm_error.reason  # 사유 코드만, 원문 없음
    assert adapter.attempt_usage == [{"error": llm_error.reason, "elapsed_ms": 3000}]


def test_adapter_shrinks_call_timeout_to_remaining_budget(engine_errors):
    clock = Clock()
    error = LlmCallError(REASON_RATE_LIMIT, model="openai/gpt-x", status_code=429)
    llm = FakeLlm([error, '{"ok": true}'], clock, took=15.0)
    adapter = _adapter(llm, clock, call_timeout=12.0, budget=20.0)

    with pytest.raises(_EngineCallError):
        adapter.complete("PROMPT", REQUEST)
    adapter.complete("PROMPT", REQUEST)

    assert [call["timeout_seconds"] for call in llm.calls] == [12.0, 5.0]


def test_adapter_stops_calling_when_budget_is_spent(engine_errors):
    clock = Clock()
    timeout = LlmTimeoutError(REASON_TIMEOUT, model="openai/gpt-x")
    llm = FakeLlm([timeout], clock, took=20.0)
    adapter = _adapter(llm, clock, budget=20.0)

    with pytest.raises(_EngineTimeout):
        adapter.complete("PROMPT", REQUEST)
    for _ in range(2):  # 엔진의 2·3번째 시도: 부르지 않고 시간 초과
        with pytest.raises(_EngineTimeout) as caught:
            adapter.complete("PROMPT", REQUEST)
        assert str(caught.value) == A.BUDGET_EXHAUSTED

    assert len(llm.calls) == 1
    assert [usage["error"] for usage in adapter.attempt_usage] == [
        REASON_TIMEOUT,
        A.BUDGET_EXHAUSTED,
        A.BUDGET_EXHAUSTED,
    ]


def test_adapter_marks_attempts_that_never_reached_the_provider(engine_errors):
    """시간 예산이 떨어져 부르지 않은 시도는 requested=False 다(호출 수·비용에 넣지 않음)."""
    clock = Clock()
    timeout = LlmTimeoutError(REASON_TIMEOUT, model="openai/gpt-x")
    adapter = _adapter(FakeLlm([timeout], clock, took=20.0), clock, budget=20.0)

    for _ in range(2):
        with pytest.raises(_EngineTimeout):
            adapter.complete("PROMPT", REQUEST)

    assert adapter.attempt_usage == [
        {"error": REASON_TIMEOUT, "elapsed_ms": 20000},
        {"error": A.BUDGET_EXHAUSTED, "elapsed_ms": 0, "requested": False},
    ]


def test_adapter_records_cached_tokens_when_reported(engine_errors):
    def answer(**kwargs):
        return LlmResult(
            text="{}",
            model=kwargs["model"],
            response_model=None,
            usage=LlmUsage(
                input_tokens=2000, output_tokens=300, cached_input_tokens=1536
            ),
            elapsed_ms=7,
        )

    adapter = _adapter(answer)
    adapter.complete("PROMPT", REQUEST)

    assert adapter.attempt_usage == [
        {
            "input_tokens": 2000,
            "output_tokens": 300,
            "cached_input_tokens": 1536,
            "elapsed_ms": 7,
        }
    ]


def test_adapter_keeps_token_counts_of_billed_failures(engine_errors):
    """잘린 응답은 엔진에는 호출 실패지만 제공자가 청구한다. 토큰 수가 사용량에 남는다."""
    clock = Clock()
    truncated = LlmCallError(
        REASON_TRUNCATED,
        model="openai/gpt-x",
        usage=LlmUsage(input_tokens=2000, output_tokens=4096),
    )
    adapter = _adapter(FakeLlm([truncated], clock, took=3.0), clock)

    with pytest.raises(_EngineCallError):
        adapter.complete("PROMPT", REQUEST)

    assert adapter.attempt_usage == [
        {
            "error": REASON_TRUNCATED,
            "input_tokens": 2000,
            "output_tokens": 4096,
            "elapsed_ms": 3000,
        }
    ]


def test_budget_starts_at_first_call_not_at_creation(engine_errors):
    clock = Clock()
    llm = FakeLlm(['{"ok": true}'], clock)
    adapter = _adapter(llm, clock, budget=20.0)
    clock.now += 500.0  # 만든 뒤 한참 있다가 불러도

    adapter.complete("PROMPT", REQUEST)

    assert llm.calls[0]["timeout_seconds"] == 12.0


def test_adapter_uses_shared_llm_layer_by_default(engine_errors, monkeypatch):
    llm = FakeLlm(['{"ok": true}'])
    monkeypatch.setattr(A, "complete_text", llm)
    adapter = A.ExplanationModelAdapter(
        provider="openai",
        model_id="gpt-x",
        api_key=None,
        call_timeout_seconds=1.0,
        total_budget_seconds=2.0,
    )

    adapter.complete("PROMPT", REQUEST)

    assert len(llm.calls) == 1


# --- 3. 고르기·서버 시작 확인 (엔진 없이) -----------------------------------------------------


def test_real_model_gets_a_new_adapter_each_time():
    settings = _real_settings(glowship_engine_dir=None)

    first = S.new_explanation_adapter(settings)
    second = S.new_explanation_adapter(settings)

    assert isinstance(first, A.ExplanationModelAdapter)
    assert first is not second
    assert (first.provider, first.model_id) == ("openai", TEST_MODEL_ID)
    assert first.attempt_usage is not second.attempt_usage


def test_model_id_may_contain_slashes():
    settings = _real_settings("bedrock/anthropic.claude/v1", openai_api_key=None)

    adapter = S.new_explanation_adapter(settings)

    assert (adapter.provider, adapter.model_id) == ("bedrock", "anthropic.claude/v1")


@pytest.mark.parametrize(
    ("model", "keys", "message"),
    [
        ("openai/gpt-x", {"openai_api_key": None}, "OPENAI_API_KEY"),
        ("gemini/gemini-x", {}, "GEMINI_API_KEY"),  # 다른 제공자 키만 있음
        ("anthropic/claude-x", {}, "ANTHROPIC_API_KEY"),
        ("azure/gpt-x", {}, "모르는 모델 제공자"),
        ("mock/other", {}, "mock 모델은"),
    ],
)
def test_real_model_needs_known_provider_and_its_key(model, keys, message):
    settings = _real_settings(model, glowship_engine_dir=Path("C:/engine"), **keys)

    with pytest.raises(S.ExplanationModelConfigError, match=message) as caught:
        S.check_explanation_model_ready(settings)
    assert API_KEY not in str(caught.value)
    with pytest.raises(S.ExplanationModelConfigError):
        S.new_explanation_adapter(settings)


def test_startup_check_does_nothing_for_mock():
    assert S.check_explanation_model_ready(_settings()) is False


def test_startup_check_stops_real_model_without_engine_dir():
    settings = _real_settings(glowship_engine_dir=None)

    with pytest.raises(S.ExplanationModelConfigError, match="GLOWSHIP_ENGINE_DIR"):
        S.check_explanation_model_ready(settings)


async def test_lifespan_stops_before_auto_start_when_model_check_fails(monkeypatch):
    calls: list[str] = []

    def failing_check(_settings):
        calls.append("model")
        raise S.ExplanationModelConfigError("키 없음")

    monkeypatch.setattr(
        app_main, "verify_engine_on_startup", lambda: calls.append("engine")
    )
    monkeypatch.setattr(app_main, "check_explanation_model_ready", failing_check)
    monkeypatch.setattr(
        app_main, "check_auto_start_ready", lambda _settings: calls.append("auto")
    )
    monkeypatch.setattr(app_main, "get_analysis_runner", lambda: calls.append("runner"))

    with pytest.raises(S.ExplanationModelConfigError):
        async with lifespan(app):
            pass

    assert calls == ["engine", "model"]


def test_mock_server_does_not_load_llm_library():
    """기본(mock) 서버는 앱을 불러오고 시작 확인을 해도 모델 호출 라이브러리를 메모리에 올리지 않는다."""
    code = (
        "import sys\n"
        "import app.main\n"
        "from app.core.config import get_settings\n"
        "from app.modules.delay_analysis.infrastructure.explanation_models import selection\n"
        "assert selection.check_explanation_model_ready(get_settings()) is False\n"
        "loaded = sorted(n for n in sys.modules if n.split('.')[0] in ('litellm', 'openai', 'tiktoken'))\n"
        "print('LOADED', loaded)\n"
    )
    env = {**os.environ, "EXPLANATION_MODEL": "", "PYTHONUTF8": "1"}

    done = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=Path(__file__).resolve().parents[3],
        timeout=120,
    )

    assert done.returncode == 0, done.stderr[-2000:]
    assert "LOADED []" in done.stdout


# --- 4. 엔진과 함께 -------------------------------------------------------------------------


@pytest.fixture(scope="module")
def engine():
    if get_settings().glowship_engine_dir is None:
        pytest.skip("GLOWSHIP_ENGINE_DIR 이 없어 설명 모델 연결 시험을 건너뜀")
    return {
        "pipeline": load_engine_module("pipeline"),
        "generator": load_engine_module("explain.generator"),
        "assets": load_engine_module("explain.assets"),
        "adapter": load_engine_module("explain.adapter"),
        "mock": load_engine_module("explain.mock"),
        "gs05": load_engine_test_module("fixture_gs05"),
        "labels": XA.load_explanation_assets()["labels"],
    }


@pytest.fixture
def gs05(engine, monkeypatch):
    # canary 고정: 16진 난수가 우연히 금지 패턴에 맞으면 사유 코드가 늘어난다(test_explanation.py 와 같은 이유)
    monkeypatch.setattr(engine["generator"].secrets, "token_hex", lambda n: "ab" * n)
    snap, _ = engine["gs05"].build_snap()
    return snap, engine["pipeline"].analyze(snap)


@pytest.fixture
def registered_model(engine, tmp_path, monkeypatch):
    """엔진 자산 복사본에 시험용 실제 모델 설정을 등록한다(엔진 원본은 그대로). 데이터 팀 새 엔진 판이 할 일을 흉내 낸다."""
    base = tmp_path / "assets"
    shutil.copytree(engine["assets"].ASSETS, base)
    config = {
        "schema_version": "model_config.v1",
        "config_id": TEST_MODEL_ID,
        "provider": "openai",
        "model_id": TEST_MODEL_ID,
        "network": True,
        "settings": {},
    }
    path = base / "model_configs" / f"{TEST_MODEL_ID}.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    registry_path = base / "registry_v2.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["model_configs"].append(
        {
            "config_id": TEST_MODEL_ID,
            "path": f"model_configs/{TEST_MODEL_ID}.json",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    )
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    monkeypatch.setattr(engine["assets"], "ASSETS", base)
    return TEST_MODEL


class ScriptedModels:
    """
    selection 이 만드는 어댑터를, 가짜 모델 호출을 쓰는 어댑터로 바꾼다. script 항목: "ok"(검증을 통과하는 설명) 또는 예외.
    통과하는 설명은 엔진 mock 의 템플릿으로 만든다(메타데이터는 그 시도의 것이라 모델 이름이 실제 모델로 들어간다).
    """

    def __init__(self, engine, monkeypatch, script, took=0.0):
        owner = self
        self.instances: list[A.ExplanationModelAdapter] = []
        self.calls: list[dict] = []
        self.clock = Clock()

        class Scripted(A.ExplanationModelAdapter):
            def __init__(self, **kwargs):
                super().__init__(**kwargs, complete=self._answer, clock=owner.clock)
                self.script = list(script)
                owner.instances.append(self)

            def complete(self, prompt, request):
                self.request = request
                return super().complete(prompt, request)

            def _answer(self, **kwargs):
                owner.calls.append(kwargs)
                owner.clock.now += took
                step = self.script.pop(0)
                if isinstance(step, Exception):
                    raise step
                rendered = engine["mock"].render(
                    self.request["input_document"],
                    engine["labels"],
                    self.request["metadata"],
                )
                return LlmResult(
                    text=json.dumps(rendered, ensure_ascii=False),
                    model=kwargs["model"],
                    response_model=None,
                    usage=LlmUsage(input_tokens=1200, output_tokens=900),
                    elapsed_ms=int(took * 1000),
                )

        monkeypatch.setattr(S, "ExplanationModelAdapter", Scripted)


def _use_settings(monkeypatch, settings):
    monkeypatch.setattr(S, "get_settings", lambda: settings)


class FakeTracer:
    def __init__(self):
        self.calls: list[tuple[list[dict], list]] = []
        self.usage_calls: list[tuple[str, dict]] = []

    def explanation_attempts(self, attempts, usage=()):
        self.calls.append(([dict(a) for a in attempts], list(usage)))

    def model_usage(self, feature, usage):
        self.usage_calls.append((feature, dict(usage)))


def _explain(gs05, tracer=None):
    snap, analysis = gs05
    return X.generate_explanation(
        snap=snap,
        analysis=analysis,
        analysis_run_id=RUN_ID,
        shipment_code=SHIP,
        now=NOW,
        tracer=tracer,
    )


def _timeout():
    return LlmTimeoutError(REASON_TIMEOUT, model=TEST_MODEL)


def _rate_limited():
    return LlmCallError(REASON_RATE_LIMIT, model=TEST_MODEL, status_code=429)


def test_mock_is_still_the_default_adapter(engine):
    first, second = X.default_adapter(), X.default_adapter()

    assert type(first) is engine["mock"].MockAdapter
    assert first is not second
    assert (first.provider, first.model_id) == ("mock", "mock-template-v1")


def test_registered_real_model_passes_through_engine(
    engine, gs05, registered_model, monkeypatch
):
    models = ScriptedModels(engine, monkeypatch, ["ok"], took=2.0)
    _use_settings(monkeypatch, _real_settings())
    tracer = FakeTracer()

    result = asyncio.run(_explain(gs05, tracer))

    assert isinstance(result, X.ExplanationPassed)
    assert result.model_name == TEST_MODEL
    assert result.final_explanation_json["model_provider"] == "openai"
    assert result.final_explanation_json["model_id"] == TEST_MODEL_ID
    assert [a["retry_no"] for a in result.attempts] == [1]
    (call,) = models.calls
    assert call["model"] == TEST_MODEL
    assert call["api_key"].get_secret_value() == API_KEY
    assert call["response_format"] == {"type": "json_object"}
    assert "CNRY-" in call["prompt"]  # 엔진이 만든 프롬프트 그대로
    # 시도별 사용량이 추적기의 사용량 칸으로 간다
    ((attempts, usage),) = tracer.calls
    assert len(attempts) == 1
    assert usage == [{"input_tokens": 1200, "output_tokens": 900, "elapsed_ms": 2000}]


def test_call_failures_count_as_engine_attempts(
    engine, gs05, registered_model, monkeypatch
):
    """공용 층의 두 오류가 엔진이 잡는 바로 그 클래스로 바뀐다(아니면 생성기를 뚫고 나가 실행이 예기치 않은 오류로 끝남)."""
    models = ScriptedModels(
        engine, monkeypatch, [_timeout(), _rate_limited(), _timeout()]
    )
    _use_settings(monkeypatch, _real_settings())

    result = asyncio.run(_explain(gs05))

    assert isinstance(result, X.ExplanationFailed)
    assert result.failure_message == (
        "1:MODEL_TIMEOUT@adapter;2:MODEL_CALL_ERROR@adapter;3:MODEL_TIMEOUT@adapter"
    )
    assert result.model_name == TEST_MODEL
    assert len(models.calls) == 3
    assert result.failure_summary().failure_code == "EXPLANATION_GENERATION_FAILED"


def test_failure_then_success_passes_on_second_attempt(
    engine, gs05, registered_model, monkeypatch
):
    models = ScriptedModels(engine, monkeypatch, [_rate_limited(), "ok"])
    _use_settings(monkeypatch, _real_settings())
    tracer = FakeTracer()

    result = asyncio.run(_explain(gs05, tracer))

    assert isinstance(result, X.ExplanationPassed)
    assert [a["retry_no"] for a in result.attempts] == [1, 2]
    assert [a["codes"] for a in result.attempts] == [
        [["MODEL_CALL_ERROR", "adapter"]],
        [],
    ]
    ((_, usage),) = tracer.calls
    assert [sorted(item) for item in usage] == [
        ["elapsed_ms", "error"],
        ["elapsed_ms", "input_tokens", "output_tokens"],
    ]
    assert len(models.calls) == 2


def test_spent_budget_ends_remaining_attempts_without_calls(
    engine, gs05, registered_model, monkeypatch
):
    """첫 호출이 총 시간 예산(20초)을 다 쓰면 남은 두 시도는 모델을 부르지 않고 시간 초과로 끝난다."""
    models = ScriptedModels(engine, monkeypatch, [_timeout()], took=20.0)
    _use_settings(monkeypatch, _real_settings())

    result = asyncio.run(_explain(gs05))

    assert isinstance(result, X.ExplanationFailed)
    assert result.failure_message == (
        "1:MODEL_TIMEOUT@adapter;2:MODEL_TIMEOUT@adapter;3:MODEL_TIMEOUT@adapter"
    )
    assert len(models.calls) == 1
    assert [u["error"] for u in models.instances[0].attempt_usage] == [
        REASON_TIMEOUT,
        A.BUDGET_EXHAUSTED,
        A.BUDGET_EXHAUSTED,
    ]


def test_each_explanation_gets_its_own_adapter(
    engine, gs05, registered_model, monkeypatch
):
    """동시에 도는 설명 두 건이 어댑터를 같이 쓰지 않는다(총 시간 예산·시도별 사용량이 섞이지 않음)."""
    models = ScriptedModels(engine, monkeypatch, ["ok"])
    _use_settings(monkeypatch, _real_settings())

    async def both():
        return await asyncio.gather(_explain(gs05), _explain(gs05))

    first, second = asyncio.run(both())

    assert isinstance(first, X.ExplanationPassed)
    assert isinstance(second, X.ExplanationPassed)
    assert len(models.instances) == 2
    assert models.instances[0] is not models.instances[1]
    assert [len(i.attempt_usage) for i in models.instances] == [1, 1]


def test_explainer_from_runtime_builds_adapter_per_run(
    engine, gs05, registered_model, monkeypatch
):
    """실행 묶음은 make_explainer() 를 어댑터 없이 한 번 부른다. 그 explain 을 두 번 불러도 어댑터는 건마다 새것이다."""
    models = ScriptedModels(engine, monkeypatch, ["ok"])
    _use_settings(monkeypatch, _real_settings())
    snap, analysis = gs05
    run_id = uuid.UUID(RUN_ID)
    ship_id = uuid.UUID("99999999-2222-3333-4444-555555555555")
    engine_input = SimpleNamespace(
        analysis_run_id=run_id,
        shipment_id=ship_id,
        snap=snap,
        analysis=analysis,
        store=SimpleNamespace(ships={str(ship_id): {"shipment_code": SHIP}}),
    )
    ctx = SimpleNamespace(analysis_run_id=run_id, engine=engine_input, tracer=None)
    explain = X.make_explainer()

    for _ in range(2):
        result = asyncio.run(explain(ctx, NOW))
        assert isinstance(result, X.ExplanationPassed)

    assert len(models.instances) == 2


def test_unregistered_model_stops_before_any_call(engine, gs05, monkeypatch):
    """엔진 등록부에 없는 모델은 시도로 세지 않고, 모델을 부르기 전에 자산 오류로 멈춘다."""
    models = ScriptedModels(engine, monkeypatch, ["ok"])
    _use_settings(monkeypatch, _real_settings("openai/not-registered"))

    with pytest.raises(XA.ExplanationAssetError) as caught:
        asyncio.run(_explain(gs05))

    assert caught.value.code == "ASSET_NOT_REGISTERED"
    assert models.calls == []


def test_startup_check_passes_for_registered_model_with_key(engine, registered_model):
    assert S.check_explanation_model_ready(_real_settings()) is True


def test_startup_check_stops_on_unregistered_model(engine):
    """지금 엔진 판에는 mock 설정뿐이라, 실제 모델을 고르면 서버가 뜨지 않는다(의도한 안전장치)."""
    with pytest.raises(XA.ExplanationAssetError) as caught:
        S.check_explanation_model_ready(_real_settings("openai/gpt-x"))
    assert caught.value.code == "ASSET_NOT_REGISTERED"


def test_startup_check_stops_when_provider_differs_from_registry(
    engine, registered_model
):
    settings = _real_settings(f"gemini/{TEST_MODEL_ID}", gemini_api_key=API_KEY)

    with pytest.raises(XA.ExplanationAssetError) as caught:
        S.check_explanation_model_ready(settings)
    assert caught.value.code == "MODEL_CONFIG_MISMATCH"


def test_engine_errors_are_the_classes_the_generator_catches(engine):
    generator = engine["generator"]

    assert engine["adapter"].ModelTimeout is generator.ModelTimeout
    assert engine["adapter"].ModelCallError is generator.ModelCallError


# --- 6. 모델 사용량 기록 (NFR-008, 엔진과 함께) ------------------------------------------------


def _usage_lines(capsys) -> list[dict]:
    """표준 출력에 남은 사용량 로그 줄(JSON)"""
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    return [json.loads(line) for line in lines if '"model_usage"' in line]


def test_real_model_usage_goes_to_tracer_and_server_log(
    engine, gs05, registered_model, monkeypatch, capsys
):
    """설명 한 건의 모델명·호출 수·토큰 수·예상 비용이 추적 사건 하나와 서버 로그 한 줄로 남는다."""
    ScriptedModels(engine, monkeypatch, ["ok"], took=2.0)
    _use_settings(monkeypatch, _real_settings())
    # 시험용 모델에 단가를 준다(실제 표는 그대로). 입력 0.75 · 출력 4.50 USD / 100만 토큰
    prices = {**U.MODEL_PRICES, TEST_MODEL: U.MODEL_PRICES["openai/gpt-5.4-mini"]}
    monkeypatch.setattr(U, "MODEL_PRICES", prices)
    tracer = FakeTracer()

    result = asyncio.run(_explain(gs05, tracer))

    assert isinstance(result, X.ExplanationPassed)
    ((feature, summary),) = tracer.usage_calls
    assert feature == "explain"
    assert summary["model"] == TEST_MODEL
    assert (summary["calls"], summary["calls_failed"]) == (1, 0)
    assert (summary["input_tokens"], summary["output_tokens"]) == (1200, 900)
    assert summary["estimated_cost_usd"] == "0.004950"  # 1200*0.75 + 900*4.50 = 4950
    assert summary["cost_complete"] is True
    assert summary["price"]["source"] == U.PRICE_SOURCE
    (line,) = _usage_lines(capsys)
    assert line["analysis_run_id"] == RUN_ID
    assert line["feature"] == "explain"
    assert {k: line[k] for k in summary} == summary


def test_usage_counts_failed_calls_and_skips_unsent_attempts(
    engine, gs05, registered_model, monkeypatch
):
    """실패한 호출도 호출 수에 들어가고, 시간 예산이 떨어져 부르지 않은 시도는 들어가지 않는다."""
    ScriptedModels(engine, monkeypatch, [_timeout()], took=20.0)
    _use_settings(monkeypatch, _real_settings())
    tracer = FakeTracer()

    result = asyncio.run(_explain(gs05, tracer))

    assert isinstance(result, X.ExplanationFailed)
    assert len(result.attempts) == 3  # 엔진 시도 기록은 3번 그대로
    ((_, summary),) = tracer.usage_calls
    assert (summary["calls"], summary["calls_failed"]) == (1, 1)
    assert summary["calls_without_usage"] == 1
    assert summary["input_tokens"] is None
    assert summary["cost_complete"] is False


def test_unpriced_model_records_tokens_without_cost(
    engine, gs05, registered_model, monkeypatch
):
    ScriptedModels(engine, monkeypatch, [_rate_limited(), "ok"])
    _use_settings(monkeypatch, _real_settings())
    tracer = FakeTracer()

    asyncio.run(_explain(gs05, tracer))

    ((_, summary),) = tracer.usage_calls
    assert (summary["calls"], summary["calls_failed"]) == (2, 1)
    assert (summary["input_tokens"], summary["output_tokens"]) == (1200, 900)
    assert summary["estimated_cost_usd"] is None
    assert summary["price"]["source"] == U.PRICE_SOURCE_UNKNOWN


def test_mock_usage_is_recorded_with_zero_cost(engine, gs05, capsys):
    """기본(mock)도 남긴다: 호출 수 = 시도 수, 토큰 수 없음, 금액 0, 출처 mock."""
    tracer = FakeTracer()

    result = asyncio.run(_explain(gs05, tracer))

    assert isinstance(result, X.ExplanationPassed)
    ((_, summary),) = tracer.usage_calls
    assert summary["model"] == MOCK_EXPLANATION_MODEL
    assert (summary["calls"], summary["calls_failed"]) == (1, 0)
    assert summary["input_tokens"] is None
    assert summary["estimated_cost_usd"] == "0.000000"
    assert summary["price"]["source"] == U.PRICE_SOURCE_MOCK
    (line,) = _usage_lines(capsys)
    assert line["analysis_run_id"] == RUN_ID


class AttemptsBrokenTracer(FakeTracer):
    def explanation_attempts(self, attempts, usage=()):
        raise RuntimeError("attempts down")


class UsageBrokenTracer(FakeTracer):
    def model_usage(self, feature, usage):
        raise RuntimeError("usage down")


def test_usage_is_recorded_even_when_attempt_tracing_fails(engine, gs05):
    """시도 기록과 사용량 기록은 따로 감싼다. 시도 기록이 실패해도 사용량은 남는다."""
    tracer = AttemptsBrokenTracer()

    result = asyncio.run(_explain(gs05, tracer))

    assert isinstance(result, X.ExplanationPassed)
    assert tracer.calls == []
    assert len(tracer.usage_calls) == 1


def test_broken_usage_recording_does_not_change_the_explanation(
    engine, gs05, monkeypatch, caplog
):
    """추적기·서버 로그·합계가 각각 망가져도 설명 결과는 사용량 기록이 없을 때와 같다."""
    baseline = asyncio.run(_explain(gs05))

    with caplog.at_level("WARNING"):
        traced = asyncio.run(_explain(gs05, UsageBrokenTracer()))
        monkeypatch.setattr(X, "log_model_usage", _raise)
        logged = asyncio.run(_explain(gs05, FakeTracer()))
        monkeypatch.setattr(X, "summarize_usage", _raise)
        summed = asyncio.run(_explain(gs05, FakeTracer()))

    assert traced == logged == summed == baseline
    messages = [record.getMessage() for record in caplog.records]
    assert sum("모델 사용량 추적 기록 실패" in m for m in messages) == 1
    assert sum("모델 사용량 로그 기록 실패" in m for m in messages) == 1
    assert sum("모델 사용량 합계 실패" in m for m in messages) == 1


def _raise(*args, **kwargs):
    raise RuntimeError("usage recording down")


def test_usage_never_reaches_the_saved_explanation(
    engine, gs05, registered_model, monkeypatch
):
    """사용량은 추적·로그로만 간다. 저장되는 설명·시도 기록(판정 서명 해시가 든 것)에는 토큰·비용 칸이 없다."""
    ScriptedModels(engine, monkeypatch, ["ok"])
    _use_settings(monkeypatch, _real_settings())

    result = asyncio.run(_explain(gs05, FakeTracer()))

    saved = json.dumps(
        [result.final_explanation_json, result.attempts], ensure_ascii=False
    )
    for key in ("input_tokens", "output_tokens", "estimated_cost_usd", "calls"):
        assert f'"{key}"' not in saved
    assert {field.name for field in dataclasses.fields(result)} == {
        "model_name",
        "prompt_version",
        "final_explanation_json",
        "generated_at",
        "input_document",
        "attempts",
    }
