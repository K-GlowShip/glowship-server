import asyncio
import copy
import dataclasses
import json
import uuid
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from app.core.config import get_settings
from app.modules.delay_analysis.application.agent import explanation as X
from app.modules.delay_analysis.application.agent.fake_runtime import fake_runtime
from app.modules.delay_analysis.application.agent.nodes.explain import explain_node
from app.modules.delay_analysis.application.agent.nodes.validate import validate_node
from app.modules.delay_analysis.application.agent.retry import (
    EXPLANATION_GENERATION_FAILED,
    ToolFailure,
)
from app.modules.delay_analysis.application.agent.runtime import (
    RUNTIME_KEY,
    ExplanationFailedLike,
)
from app.modules.delay_analysis.application.agent.state import (
    AnalysisOutcome,
    AnalysisStep,
)
from app.modules.delay_analysis.application.agent.tools.common import ToolName
from app.modules.delay_analysis.application.agent.tools.context import ToolContext
from app.modules.delay_analysis.application.agent.tools.output_validation import (
    validate_agent_output,
)
from app.modules.delay_analysis.application.agent.tools.registry import (
    ToolSpec,
    pure_tool,
)
from app.modules.delay_analysis.application.agent.tools.validation_schemas import (
    AgentOutputStatus,
    AgentOutputValidation,
    ValidateAgentOutputInput,
)
from app.modules.delay_analysis.infrastructure.engine import explanation_assets as XA
from app.modules.delay_analysis.infrastructure.engine.engine_input import EngineInput
from app.modules.delay_analysis.infrastructure.engine.engine_loader import (
    load_engine_module,
)

"""
    설명 생성을 그래프 실행 묶음에 잇는 감싸기 (GLOW-127 후속, DV3-3 해소, docs/agent-flow/explanation.md 2절)
      1. make_explainer()(ctx, now) = generate_explanation 을 ctx.engine 값으로 직접 부른 결과
      2. 지연 선적 코드는 엔진 저장소 선적 표에서. 실행 ID 가 어긋나거나 코드가 없으면 모델을 부르기 전에 연결 오류
      3. 추적은 설명 시도 기록을 받는 추적기에만(도구 시도만 받는 추적기면 건너뜀)
      4. final_check_input 으로 만든 T12 입력이 통과 설명은 PASSED, 바꾼 설명은 잡음
      5. 실제 설명 노드·결과 검증 노드(실제 T12 등록)로 통과 → READY_FOR_REVIEW, 3번 실패 → FAILED
"""

RUN_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
COMPANY_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c1")
SHIPMENT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
SHIP = "SHIP-DELAYED"
NOW = datetime(2026, 3, 10, 0, 5, tzinfo=UTC)
FAULTS = {1: "wrong_number", 2: "forbidden_phrase", 3: "timeout"}  # 엔진 RT2_FAULTS


@pytest.fixture(scope="module")
def engine():
    if get_settings().glowship_engine_dir is None:
        pytest.skip("GLOWSHIP_ENGINE_DIR 이 없어 설명 연결 시험을 건너뜀")
    return {
        "pipeline": load_engine_module("pipeline"),
        "generator": load_engine_module("explain.generator"),
        "mock": load_engine_module("explain.mock"),
        "gs05": load_engine_module("tests.fixture_gs05"),
        "labels": XA.load_explanation_assets()["labels"],
    }


@pytest.fixture
def engine_input(engine, monkeypatch):
    """GS-05(READY) 를 실제 EngineInput 모양으로. canary 는 test_explanation.py 와 같은 이유로 고정한다."""
    monkeypatch.setattr(engine["generator"].secrets, "token_hex", lambda n: "ab" * n)
    snap, _ = engine["gs05"].build_snap()
    return EngineInput(
        analysis_run_id=RUN_ID,
        company_id=COMPANY_ID,
        delay_event_id=uuid.UUID(int=7),
        shipment_id=SHIPMENT_ID,
        base_date=date(2026, 3, 10),
        input_seq=1,
        confirm_t="2026-03-10T00:00:00Z",
        snap=snap,
        analysis=engine["pipeline"].analyze(snap),
        store=SimpleNamespace(ships={str(SHIPMENT_ID): {"shipment_code": SHIP}}),
    )


def _ctx(engine_input, *, tracer=None, run_id=RUN_ID):
    return ToolContext(
        company_id=COMPANY_ID,
        analysis_run_id=run_id,
        base_date=engine_input.base_date,
        input_seq=engine_input.input_seq,
        engine=engine_input,
        tracer=tracer,
    )


class CountingAdapter:
    """모델을 몇 번 불렀는지 센다(연결 오류면 0번이어야 함)."""

    def __init__(self, inner):
        self.inner, self.calls = inner, 0
        self.provider, self.model_id = inner.provider, inner.model_id

    def complete(self, prompt, request):
        self.calls += 1
        return self.inner.complete(prompt, request)


class AttemptTracer:
    def __init__(self):
        self.calls = []

    def explanation_attempts(self, attempts, usage=()):
        self.calls.append([dict(a) for a in attempts])

    def tool_attempt(self, record):
        pass


class ToolOnlyTracer:
    """ToolContext 가 정한 모양(도구 시도만). 설명 시도 기록은 받지 않는다."""

    def tool_attempt(self, record):
        pass


def _explain(ctx, adapter=None):
    return asyncio.run(X.make_explainer(adapter=adapter)(ctx, NOW))


def _bump_number(explanation: dict) -> dict:
    changed = copy.deepcopy(explanation)
    changed["risk_summary"] = changed["risk_summary"].replace(
        "2개입니다", "3개입니다", 1
    )
    assert changed != explanation
    return changed


# --- 1~3. 감싸기 --------------------------------------------------------------------------------


def test_explainer_equals_direct_generate_call(engine_input):
    direct = asyncio.run(
        X.generate_explanation(
            snap=engine_input.snap,
            analysis=engine_input.analysis,
            analysis_run_id=str(RUN_ID),
            shipment_code=SHIP,
            now=NOW,
        )
    )
    wrapped = _explain(_ctx(engine_input))
    assert isinstance(wrapped, X.ExplanationPassed)
    assert wrapped == direct
    assert wrapped.input_document["run"]["delayed_shipment_code"] == SHIP
    assert wrapped.input_document["run"]["analysis_run_id"] == str(RUN_ID)


def test_explainer_reads_engine_values_without_changing_them(engine_input):
    before = copy.deepcopy((engine_input.snap, engine_input.analysis))
    _explain(_ctx(engine_input))
    assert (engine_input.snap, engine_input.analysis) == before


def test_shipment_code_comes_from_store_ship_table(engine_input):
    other = dataclasses.replace(
        engine_input,
        store=SimpleNamespace(
            ships={str(SHIPMENT_ID): {"shipment_code": "SHIP-OTHER"}}
        ),
    )
    assert X.delayed_shipment_code(other) == "SHIP-OTHER"
    run = _explain(_ctx(other)).input_document["run"]
    assert run["delayed_shipment_code"] == "SHIP-OTHER"


@pytest.mark.parametrize(
    "store",
    [
        None,
        SimpleNamespace(ships={}),
        SimpleNamespace(ships={str(SHIPMENT_ID): {"shipment_code": ""}}),
        SimpleNamespace(ships={SHIPMENT_ID: {"shipment_code": SHIP}}),
    ],
    ids=["no_store", "no_row", "empty_code", "uuid_key_not_engine_shape"],
)
def test_missing_shipment_code_is_connection_error_before_model_call(
    engine, engine_input, store
):
    adapter = CountingAdapter(engine["mock"].MockAdapter(engine["labels"]))
    ctx = _ctx(dataclasses.replace(engine_input, store=store))
    with pytest.raises(X.ExplanationInputError, match="선적"):
        _explain(ctx, adapter)
    assert adapter.calls == 0


def test_run_id_mismatch_is_connection_error_before_model_call(engine, engine_input):
    adapter = CountingAdapter(engine["mock"].MockAdapter(engine["labels"]))
    with pytest.raises(X.ExplanationInputError, match="실행"):
        _explain(_ctx(engine_input, run_id=uuid.UUID(int=99)), adapter)
    assert adapter.calls == 0


def test_tracer_with_explanation_attempts_receives_them(engine_input):
    tracer = AttemptTracer()
    result = _explain(_ctx(engine_input, tracer=tracer))
    assert tracer.calls == [list(result.attempts)]


def test_tool_only_tracer_is_skipped(engine_input, caplog):
    """도구 시도만 받는 추적기면 넘기지 않는다(넘기면 추적 실패 경고가 남음)."""
    with caplog.at_level("WARNING"):
        with_tool_tracer = _explain(_ctx(engine_input, tracer=ToolOnlyTracer()))
    assert with_tool_tracer == _explain(_ctx(engine_input))
    assert "설명 시도 추적 기록 실패" not in caplog.text


def test_three_faults_give_failure_like_graph_expects(engine, engine_input):
    adapter = engine["mock"].MockAdapter(engine["labels"], FAULTS)
    result = _explain(_ctx(engine_input), adapter)
    assert isinstance(result, X.ExplanationFailed)
    assert isinstance(result, ExplanationFailedLike)  # 그래프 runtime 의 Protocol
    summary = result.failure_summary()
    assert summary.failure_code == EXPLANATION_GENERATION_FAILED
    assert summary.failed_step == AnalysisStep.VALIDATING_OUTPUT
    assert (summary.attempt_count, summary.retry_exhausted) == (3, True)


# --- 4. T12 입력 --------------------------------------------------------------------------------


def test_final_check_input_passes_t12(engine_input):
    passed = _explain(_ctx(engine_input))
    data = X.final_check_input(passed)
    assert data.canary is None
    assert data.expected_metadata == passed.expected_metadata()
    assert json.loads(data.output) == passed.final_explanation_json
    check = validate_agent_output(data)
    assert (check.status, check.errors) == (AgentOutputStatus.PASSED, [])


def test_final_check_input_of_changed_explanation_is_caught(engine_input):
    passed = _explain(_ctx(engine_input))
    changed = dataclasses.replace(
        passed, final_explanation_json=_bump_number(passed.final_explanation_json)
    )
    check = validate_agent_output(X.final_check_input(changed))
    assert check.status is not AgentOutputStatus.PASSED
    assert "VALUE_MISMATCH" in {e.code for e in check.errors}


# --- 5. 실제 설명 노드·결과 검증 노드 ---------------------------------------------------------------


def _runtime(engine_input, adapter=None):
    """가짜 묶음에 설명·T12 입력·실제 T12 만 바꿔 끼우고, 엔진 입력은 GS-05 로 시작한다."""
    runtime = fake_runtime()
    runtime.explain = X.make_explainer(adapter=adapter)
    runtime.final_check_input = X.final_check_input
    runtime.registry = {
        **runtime.registry,
        ToolName.VALIDATE_AGENT_OUTPUT: ToolSpec(
            pure_tool(validate_agent_output),
            ValidateAgentOutputInput,
            AgentOutputValidation,
        ),
    }
    runtime.start(company_id=COMPANY_ID, analysis_run_id=RUN_ID, engine=engine_input)
    return runtime, {"configurable": {RUNTIME_KEY: runtime}}


def test_nodes_with_real_explanation_end_ready(engine_input):
    runtime, config = _runtime(engine_input)
    explained = asyncio.run(explain_node({}, config))
    assert isinstance(explained["explanation"], X.ExplanationPassed)
    assert "outcome" not in explained
    # 생성 시각은 실행 묶음의 시계가 정한다
    assert explained["explanation"].generated_at == runtime.clock()
    checked = asyncio.run(validate_node(explained, config))
    assert checked["outcome"] == AnalysisOutcome.READY_FOR_REVIEW


def test_nodes_with_three_faults_end_failed(engine, engine_input):
    _, config = _runtime(
        engine_input, engine["mock"].MockAdapter(engine["labels"], FAULTS)
    )
    explained = asyncio.run(explain_node({}, config))
    assert explained["outcome"] == AnalysisOutcome.FAILED
    assert explained["failure"].failure_code == EXPLANATION_GENERATION_FAILED
    assert isinstance(explained["explanation_failed"], X.ExplanationFailed)


def test_validate_node_runs_real_t12_on_changed_explanation(engine_input):
    """음성: 결과 검증 노드가 가짜가 아니라 실제 T12 로 다시 본다(바꾼 설명은 연결 오류로 멈춤)."""
    _, config = _runtime(engine_input)
    passed = asyncio.run(explain_node({}, config))["explanation"]
    changed = dataclasses.replace(
        passed, final_explanation_json=_bump_number(passed.final_explanation_json)
    )
    with pytest.raises(ToolFailure):
        asyncio.run(validate_node({"explanation": changed}, config))
