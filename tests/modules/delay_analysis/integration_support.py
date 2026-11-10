import asyncio
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import get_settings
from app.modules.delay_analysis.application.agent import explanation as X
from app.modules.delay_analysis.application.agent.fake_runtime import (
    FakeHold,
    FakeScenario,
    fake_runtime,
    fake_tools,
)
from app.modules.delay_analysis.application.agent.graph import build_analysis_graph
from app.modules.delay_analysis.application.agent.retry import (
    EXPLANATION_GENERATION_FAILED,
    MISSING_REQUIRED_DATA,
    PERMISSION_DENIED,
    REQUIRED_DATA_INVALID,
    TOOL_TEMPORARY_ERROR_EXHAUSTED,
    TOOL_TIMEOUT_EXHAUSTED,
    UNEXPECTED_TOOL_ERROR,
    PermissionDeniedError,
    RequiredDataError,
    TransientToolError,
)
from app.modules.delay_analysis.application.agent.runtime import (
    RUNTIME_KEY,
    AnalysisRuntime,
)
from app.modules.delay_analysis.application.agent.state import (
    AnalysisOutcome,
    AnalysisStep,
)
from app.modules.delay_analysis.application.agent.tools.common import ToolName
from app.modules.delay_analysis.application.agent.tools.option_schemas import (
    OptionOutcome,
    OptionOutcomes,
    OptionRank,
    OptionRanking,
)
from app.modules.delay_analysis.application.agent.tools.output_validation import (
    validate_agent_output,
)
from app.modules.delay_analysis.application.agent.tools.registry import (
    ToolSpec,
    pure_tool,
)
from app.modules.delay_analysis.application.agent.tools.validation_schemas import (
    AgentOutputValidation,
    ValidateAgentOutputInput,
)
from app.modules.delay_analysis.application.agent.tracing import (
    RunTracer,
    TraceCallbackHandler,
)
from app.modules.delay_analysis.infrastructure.agent_trace_outbox import (
    TraceOutboxSink,
)
from app.modules.delay_analysis.infrastructure.engine import explanation_assets as XA
from app.modules.delay_analysis.infrastructure.engine.engine_input import (
    BaseDateResolution,
    EngineInput,
)
from app.modules.delay_analysis.infrastructure.engine.engine_loader import (
    load_engine_module,
)
from tests.modules.delay_analysis.engine_test_modules import (
    load_engine_test_module,
)
from tests.modules.delay_analysis.test_agent_tracing import (
    FailingSink,
    MemorySink,
    SlowSink,
)
from tests.modules.delay_analysis.test_analysis_graph import FakeRunStatusReader
from tests.modules.delay_analysis.test_explanation_runtime import (
    FAULTS as RT2_FAULTS,
)
from tests.modules.delay_analysis.test_explanation_runtime import (
    CountingAdapter,
)

"""
    통합 시험 T13(재시도 제한·실패 요약)·T20(추적 장애 독립성)의 공통 장치
    (docs/agent-flow/integration-tests-t13-t20.md). 시험 파일이 아니다.
      - 갈래 표 BRANCHES 하나를 두 시험이 같이 쓴다. T13 은 갈래마다 기대값을 보고, T20 은 같은 갈래를
        추적 장애와 함께 다시 돌려 결과가 같은지 본다.
      - 실제 그래프(build_analysis_graph)·실제 call_tool·call_with_retry 를 그대로 쓴다. 바꿔 끼우는 것은
        가짜 실행 도구 묶음(fake_runtime)의 도구·분석 준비·설명뿐이다. 운영 코드는 고치지 않는다.
      - 가짜 도구 12개는 모두 들어올 때 호출 수를 센다. 기대값은 도구 12개 전체의 정확한 수다
        (멈춘 뒤 단계의 도구는 0 = 제한 밖 호출 없음).
      - 실제 설명 갈래(엔진 필요)는 GS-05 엔진 입력을 분석 준비가 돌려주게 하고, T10·T11 가짜를 엔진 판정
        서명에서 만든다(기본 가짜는 GS-05 서명과 달라 비교 노드에서 멈춘다). 엔진 경로가 없으면 건너뛴다.
"""

T = ToolName
V, AI, AO, VO = (
    AnalysisStep.VALIDATING,
    AnalysisStep.ANALYZING_IMPACT,
    AnalysisStep.ANALYZING_OPTIONS,
    AnalysisStep.VALIDATING_OUTPUT,
)

RUN_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
COMPANY_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c1")
SHIPMENT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
# 오류 문구에 섞일 수 있는 값(접속 문자열·주문 코드). 실패 문구·추적 기록 어디에도 남으면 안 된다
SECRET = "postgresql://glowship:hunter2@db/prod A-ORD-20260613-0040"
CANARY = "7e57ca9a"  # 실제 설명 갈래의 고정 canary 조각(추적 기록에 없어야 함)
HOLD_MESSAGE = "차단 검증 오류 NEGATIVE_QUANTITY WMS-EWR01|SKU-215|2026-01-01"

# 정상 흐름이 도구를 부르는 순서(14번). test_analysis_graph 의 IMPACT_CALLS·OPTION_CALLS 와 같다
ORDER = [
    T.GET_DELAY_CONTEXT,
    T.GET_INVENTORY_SNAPSHOT,
    T.GET_INBOUND_SCHEDULE,
    T.GET_SALES_HISTORY,
    T.GET_OPEN_ORDERS,
    T.GET_OPERATING_RULES,
    T.IDENTIFY_AT_RISK_ORDERS,
    T.CALCULATE_INVENTORY_IMPACT,
    T.GET_INVENTORY_SNAPSHOT,
    T.GET_OPERATING_RULES,
    T.CHECK_OPTION_FEASIBILITY,
    T.CALCULATE_OPTION_OUTCOMES,
    T.COMPARE_OPTIONS,
    T.VALIDATE_AGENT_OUTPUT,
]


def run_input() -> dict:
    """그래프 입력. 갈래를 다시 돌려 결과를 통째로 비교하므로 ID 를 고정한다."""
    return {
        "analysis_run_id": RUN_ID,
        "company_id": COMPANY_ID,
        "delay_event_id": uuid.UUID(int=7),
        "operating_rule_set_id": uuid.UUID(int=8),
    }


# --- 오류를 내는 도구 ------------------------------------------------------------------------
SLOW = object()  # 시도별 시간 제한보다 오래 걸림
Plan = Callable[[int], Any]  # n 번째 호출(1부터) → 예외·SLOW·None(원래 도구대로)


def always(make: Callable[[], Any]) -> Plan:
    return lambda n: make()


def first(*makes: Callable[[], Any]) -> Plan:
    """앞에서부터 한 번씩 쓰고, 다 쓰면 원래 도구대로."""
    return lambda n: makes[n - 1]() if n <= len(makes) else None


def _faulty(spec: ToolSpec, plan: Plan) -> ToolSpec:
    calls = 0

    async def fn(ctx, payload):
        nonlocal calls
        calls += 1
        action = plan(calls)
        if action is SLOW:
            await asyncio.sleep(1.0)
        elif isinstance(action, Exception):
            raise action
        return await spec.fn(ctx, payload)

    return replace(spec, fn=fn)


def _counted(specs: dict[ToolName, ToolSpec], counts: Counter) -> dict:
    def wrap(name: ToolName, spec: ToolSpec) -> ToolSpec:
        async def fn(ctx, payload):
            counts[name] += 1  # 들어올 때 센다(시간 초과로 취소돼도 한 번)
            return await spec.fn(ctx, payload)

        return replace(spec, fn=fn)

    return {name: wrap(name, spec) for name, spec in specs.items()}


# --- 분석 준비 바꿔 끼우기 ---------------------------------------------------------------------
@dataclass
class StubPreparer:
    """엔진 입력 만들기가 돌려줄 것을 정한다: 엔진 입력·보류·예외."""

    built: Any

    async def judge_no_delay(self) -> None:
        return None

    async def build_engine_input(self) -> Any:
        if isinstance(self.built, Exception):
            raise self.built
        return self.built


# --- 실제 설명 갈래(엔진) ----------------------------------------------------------------------
@dataclass(frozen=True)
class EngineKit:
    snap: dict
    analysis: dict
    mock: Any
    generator: Any
    labels: Any

    def engine_input(self) -> EngineInput:
        day = date(2026, 3, 10)
        return EngineInput(
            analysis_run_id=RUN_ID,
            company_id=COMPANY_ID,
            delay_event_id=uuid.UUID(int=7),
            shipment_id=SHIPMENT_ID,
            base_date=day,
            base_date_resolution=BaseDateResolution(
                base_date=day,
                run_date=day,
                inventory_limit=None,
                order_limit=None,
                late_sources=(),
            ),
            input_seq=1,
            confirm_t="2026-03-10T00:00:00Z",
            snap=self.snap,
            analysis=self.analysis,
            store=SimpleNamespace(
                ships={str(SHIPMENT_ID): {"shipment_code": "SHIP-DELAYED"}}
            ),
        )


def load_engine_kit() -> EngineKit:
    """GS-05(READY) 세계와 엔진 판정. 엔진 경로가 없으면 그 시험을 건너뛴다."""
    if get_settings().glowship_engine_dir is None:
        pytest.skip("GLOWSHIP_ENGINE_DIR 이 없어 실제 설명 갈래를 건너뜀")
    snap, _ = load_engine_test_module("fixture_gs05").build_snap()
    return EngineKit(
        snap=snap,
        analysis=load_engine_module("pipeline").analyze(snap),
        mock=load_engine_module("explain.mock"),
        generator=load_engine_module("explain.generator"),
        labels=XA.load_explanation_assets()["labels"],
    )


def fix_canary(monkeypatch, kit: EngineKit) -> None:
    """엔진이 시도마다 만드는 canary 를 고정한다(다시 돌린 결과를 비교하고, 기록에 없는지 찾기 위해)."""
    monkeypatch.setattr(
        kit.generator.secrets, "token_hex", lambda n: (CANARY * n)[: 2 * n]
    )


async def _signed_outcomes(ctx, payload) -> OptionOutcomes:
    """T10 가짜: 엔진 판정 서명의 대응안 값 그대로(비교 노드가 서명과 대조한다)."""
    return OptionOutcomes.model_construct(
        options=[
            OptionOutcome.model_construct(
                option_type=o["option_type"],
                feasibility_status=o["feasibility_status"],
                reason_code=o["reason_code"],
                delay_induced_recovered_orders=o["delay_induced_recovered_orders"],
                reduction_committed_total=o["reduction_committed_total"],
                expected_delivery_date=None,
            )
            for o in ctx.engine.analysis["signature"]["options"]
        ]
    )


async def _signed_ranking(ctx, payload) -> OptionRanking:
    """T11 가짜: 엔진 판정 서명의 추천 그대로."""
    recommended = ctx.engine.analysis["signature"]["recommended_option_type"]
    return OptionRanking.model_construct(
        options=[
            OptionRank.model_construct(
                option_type=o.option_type,
                recommendation_rank=1 if str(o.option_type) == recommended else 2,
                is_recommended=str(o.option_type) == recommended,
            )
            for o in payload.options
        ]
    )


# --- 갈래 ----------------------------------------------------------------------------------
@dataclass
class Setup:
    """갈래 하나가 가짜 묶음에서 바꾸는 것."""

    faults: dict[ToolName, Plan] = field(default_factory=dict)
    timeout: dict[ToolName, float] = field(default_factory=dict)
    built: Any = None  # StubPreparer 가 돌려줄 것(None 이면 가짜 엔진 입력 그대로)
    explain_error: Exception | None = None  # 설명 함수가 낼 예외(도구 밖 예외)
    real_explanation: dict | None | bool = False  # 엔진 결함 표(None = 결함 없음)
    stale_on_call: int | None = None  # 이 번째 실행 상태 조회부터 STALE


@dataclass(frozen=True)
class Expected:
    outcome: AnalysisOutcome
    visited_end: str
    calls: dict[ToolName, int]
    # (실패 단계, 실패 코드, 시도 횟수, 재시도 소진). 실패·보류가 아니면 None
    failure: tuple[AnalysisStep, str, int, bool] | None = None
    explain_calls: int = 0  # 설명 함수를 부른 횟수
    model_calls: int | None = None  # 실제 설명 갈래에서 모델을 부른 횟수


@dataclass(frozen=True)
class Branch:
    name: str
    setup: Callable[[], Setup]
    expected: Expected
    needs_engine: bool = False


@dataclass
class Run:
    result: dict
    calls: dict[ToolName, int]
    explain_calls: int
    model_calls: int | None


def _stop(tool: ToolName, tries: int) -> dict[ToolName, int]:
    """tool 을 처음 부르는 자리에서 tries 번 부르고 멈췄을 때의 도구별 호출 수."""
    counts = Counter(ORDER[: ORDER.index(tool)])
    counts[tool] += tries
    return {name: n for name, n in counts.items() if n}


def _full(**extra: int) -> dict[ToolName, int]:
    """끝까지 간 흐름(14번)에 재시도로 더 부른 횟수를 더한다."""
    counts = Counter(ORDER)
    for name, more in extra.items():
        counts[T[name]] += more
    return dict(counts)


def _transient() -> Exception:
    return TransientToolError(f"원천 응답 지연 {SECRET}")


def _denied() -> Exception:
    return PermissionDeniedError(f"다른 고객사 {SECRET}")


def _failed(
    name: str,
    tool: ToolName,
    plan: Plan,
    node: str,
    step: AnalysisStep,
    code: str,
    tries: int,
    exhausted: bool,
    *,
    explain_calls: int = 0,
    timeout: float | None = None,
) -> Branch:
    return Branch(
        name,
        lambda: Setup(faults={tool: plan}, timeout={tool: timeout} if timeout else {}),
        Expected(
            AnalysisOutcome.FAILED,
            f"{node}:failed",
            _stop(tool, tries),
            (step, code, tries, exhausted),
            explain_calls,
        ),
    )


READY = AnalysisOutcome.READY_FOR_REVIEW
TEMPORARY, TIMEOUT = TOOL_TEMPORARY_ERROR_EXHAUSTED, TOOL_TIMEOUT_EXHAUSTED

BRANCHES: dict[str, Branch] = {
    b.name: b
    for b in [
        Branch("R00-정상", Setup, Expected(READY, "validate", _full(), None, 1)),
        # 정답 RT-1(E3 v2): 시도별 시간 제한을 3번 모두 넘김
        _failed(
            "R01-시간초과3회-T5",
            T.GET_OPEN_ORDERS,
            always(lambda: SLOW),
            "inventory_impact",
            AI,
            TIMEOUT,
            3,
            True,
            timeout=0.01,
        ),
        Branch(
            "R02-재시도뒤성공-T5",
            lambda: Setup(
                faults={T.GET_OPEN_ORDERS: first(lambda: TimeoutError(), _transient)}
            ),
            Expected(READY, "validate", _full(GET_OPEN_ORDERS=2), None, 1),
        ),
        Branch(  # T2 는 재고 영향·대응안 단계에서 한 번씩 부른다. 부를 때마다 2번 실패 뒤 성공
            "R03-한도는호출마다-T2",
            lambda: Setup(
                faults={
                    T.GET_INVENTORY_SNAPSHOT: lambda n: _transient() if n % 3 else None
                }
            ),
            Expected(READY, "validate", _full(GET_INVENTORY_SNAPSHOT=4), None, 1),
        ),
        _failed("R04-일시오류3회-T1", T.GET_DELAY_CONTEXT, always(_transient),
                "coordinator", AI, TEMPORARY, 3, True),
        _failed("R04-일시오류3회-T9", T.CHECK_OPTION_FEASIBILITY, always(_transient),
                "response_options", AO, TEMPORARY, 3, True),
        _failed("R04-일시오류3회-T11", T.COMPARE_OPTIONS, always(_transient),
                "compare", VO, TEMPORARY, 3, True),
        _failed("R04-일시오류3회-T12", T.VALIDATE_AGENT_OUTPUT, always(_transient),
                "validate", VO, TEMPORARY, 3, True, explain_calls=1),
        _failed("R05-권한-T1", T.GET_DELAY_CONTEXT, always(_denied),
                "coordinator", AI, PERMISSION_DENIED, 1, False),
        _failed("R05-권한-T7", T.IDENTIFY_AT_RISK_ORDERS, always(_denied),
                "inventory_impact", AI, PERMISSION_DENIED, 1, False),
        _failed("R05-권한-T11", T.COMPARE_OPTIONS, always(_denied),
                "compare", VO, PERMISSION_DENIED, 1, False),
        _failed("R05-권한-T12", T.VALIDATE_AGENT_OUTPUT, always(_denied),
                "validate", VO, PERMISSION_DENIED, 1, False, explain_calls=1),
        _failed("R06-일시오류뒤권한-T1", T.GET_DELAY_CONTEXT, first(_transient, _denied),
                "coordinator", AI, PERMISSION_DENIED, 2, False),
        # 보류는 분석 준비에서만 낸다. 도구가 필수 자료 오류를 내면 코드 오류(FAILED)
        _failed("R07-도구의필수자료오류-T10", T.CALCULATE_OPTION_OUTCOMES,
                always(lambda: RequiredDataError(MISSING_REQUIRED_DATA, "리드타임 없음")),
                "response_options", AO, UNEXPECTED_TOOL_ERROR, 1, False),
        _failed("R08-분류안된예외-T6", T.CALCULATE_INVENTORY_IMPACT,
                always(lambda: ValueError(SECRET)),
                "inventory_impact", AI, UNEXPECTED_TOOL_ERROR, 1, False),
        Branch(  # 정답 RT-2(E2 v1): 실제 설명 함수가 엔진 결함 3개로 3번 모두 실패
            "R09-설명3회실패-실제",
            lambda: Setup(real_explanation=dict(RT2_FAULTS)),
            Expected(
                AnalysisOutcome.FAILED,
                "explain",  # 설명 노드가 실패 요약을 직접 넣는다(:failed 아님)
                _stop(T.VALIDATE_AGENT_OUTPUT, 0),
                (VO, EXPLANATION_GENERATION_FAILED, 3, True),
                explain_calls=1,
                model_calls=3,
            ),
            needs_engine=True,
        ),
        Branch(  # 정답 E9 v1 과 같은 보류
            "R10-보류-REQUIRED_DATA_INVALID",
            lambda: Setup(built=FakeHold(REQUIRED_DATA_INVALID, HOLD_MESSAGE)),
            Expected(AnalysisOutcome.ON_HOLD, "prepare", {},
                     (V, REQUIRED_DATA_INVALID, 1, False)),
        ),
        Branch(
            "R10-보류-MISSING_REQUIRED_DATA",
            lambda: Setup(built=FakeHold(MISSING_REQUIRED_DATA, "출고 처리 리드타임 없음")),
            Expected(AnalysisOutcome.ON_HOLD, "prepare", {},
                     (V, MISSING_REQUIRED_DATA, 1, False)),
        ),
        Branch(
            "R11-도구밖예외-설명",
            lambda: Setup(explain_error=ConnectionError(SECRET)),
            Expected(AnalysisOutcome.FAILED, "explain:failed",
                     _stop(T.VALIDATE_AGENT_OUTPUT, 0),
                     (VO, UNEXPECTED_TOOL_ERROR, 1, False), explain_calls=1),
        ),
        Branch(
            "R11-도구밖예외-엔진입력",
            lambda: Setup(built=ConnectionError(SECRET)),
            Expected(AnalysisOutcome.FAILED, "prepare:failed", {},
                     (V, UNEXPECTED_TOOL_ERROR, 1, False)),
        ),
        Branch(  # 추적 장애 비교용: 재고 영향 뒤 조정 에이전트 진입에서 STALE 을 확인해 멈춤
            "R12-STALE중단",
            lambda: Setup(stale_on_call=2),
            Expected(AnalysisOutcome.STALE, "coordinator:stale_stop",
                     _stop(T.CALCULATE_INVENTORY_IMPACT, 1)),
        ),
    ]
}  # fmt: skip


def _runtime(
    setup: Setup, counts: Counter, tracer: Any, kit: EngineKit | None
) -> tuple[AnalysisRuntime, list[int], Any]:
    specs = fake_tools(FakeScenario())
    if setup.real_explanation is not False:
        specs[T.CALCULATE_OPTION_OUTCOMES] = replace(
            specs[T.CALCULATE_OPTION_OUTCOMES], fn=_signed_outcomes
        )
        specs[T.COMPARE_OPTIONS] = replace(specs[T.COMPARE_OPTIONS], fn=_signed_ranking)
        specs[T.VALIDATE_AGENT_OUTPUT] = ToolSpec(
            pure_tool(validate_agent_output),
            ValidateAgentOutputInput,
            AgentOutputValidation,
        )
    for name, plan in setup.faults.items():
        specs[name] = _faulty(specs[name], plan)
    for name, seconds in setup.timeout.items():
        specs[name] = replace(specs[name], timeout_seconds=seconds)

    runtime = fake_runtime(tools=_counted(specs, counts), tracer=tracer)
    adapter = None
    if setup.real_explanation is not False:
        assert kit is not None, "실제 설명 갈래는 엔진 묶음이 필요합니다"
        adapter = CountingAdapter(
            kit.mock.MockAdapter(kit.labels, setup.real_explanation)
        )
        runtime.preparer = StubPreparer(kit.engine_input())
        runtime.explain = X.make_explainer(adapter=adapter)
        runtime.final_check_input = X.final_check_input
    elif setup.built is not None:
        runtime.preparer = StubPreparer(setup.built)

    explain_calls: list[int] = []
    inner = runtime.explain

    async def explain(ctx, now):
        explain_calls.append(1)
        if setup.explain_error is not None:
            raise setup.explain_error
        return await inner(ctx, now)

    runtime.explain = explain
    return runtime, explain_calls, adapter


async def run_branch(
    branch: Branch,
    *,
    tracer: Any = None,
    callbacks: list | None = None,
    kit: EngineKit | None = None,
) -> Run:
    """갈래를 한 번 돌린다. 묶음은 실행마다 새로 만든다(묶음 하나 = 실행 하나)."""
    setup = branch.setup()
    counts: Counter = Counter()
    runtime, explain_calls, adapter = _runtime(setup, counts, tracer, kit)
    config: dict = {"configurable": {RUNTIME_KEY: runtime}}
    if setup.stale_on_call is not None:
        config["configurable"]["run_status_reader"] = FakeRunStatusReader(
            setup.stale_on_call
        )
    if callbacks:
        config["callbacks"] = callbacks
    result = await build_analysis_graph().ainvoke(run_input(), config)
    return Run(
        result=result,
        calls=dict(counts),
        explain_calls=len(explain_calls),
        model_calls=adapter.calls if adapter is not None else None,
    )


def five(failure: Any) -> tuple[AnalysisStep, str, int, bool]:
    return (
        failure.failed_step,
        failure.failure_code,
        failure.attempt_count,
        failure.retry_exhausted,
    )


# --- 추적 장애 (T20) ---------------------------------------------------------------------------
class EmitBrokenTracer(RunTracer):
    """사건 남기기 자체가 예외(추적 뒷단 장애)."""

    def emit(self, *args, **kwargs):
        raise RuntimeError("trace backend down")


class AttemptBrokenTracer(RunTracer):
    """도구 시도·설명 시도 기록 메서드가 예외."""

    def tool_attempt(self, record):
        raise RuntimeError("tool_attempt down")

    def explanation_attempts(self, attempts, usage=()):
        raise RuntimeError("explanation_attempts down")


class RaisingHandler(TraceCallbackHandler):
    """콜백 메서드가 예외. 핸들러의 raise_error = False 라 LangChain 이 삼켜야 한다."""

    def on_chain_start(self, *args, **kwargs):
        raise RuntimeError("handler start down")

    def on_chain_end(self, *args, **kwargs):
        raise RuntimeError("handler end down")

    def on_chain_error(self, *args, **kwargs):
        raise RuntimeError("handler error down")


SLOW_WRITE_SECONDS = 0.1  # F2: 사건 하나를 쓰는 데 걸리는 시간


def traced(fault: str, tmp_path: Path) -> tuple[RunTracer, list]:
    """추적 장애 하나를 건 추적기와 그래프에 넘길 콜백."""
    if fault == "F0":  # 정상
        tracer = RunTracer(RUN_ID, COMPANY_ID, [MemorySink()])
    elif fault == "F1":  # 쓰기 예외
        tracer = RunTracer(RUN_ID, COMPANY_ID, [FailingSink(), MemorySink()])
    elif fault == "F2":  # 쓰기 지연
        tracer = RunTracer(RUN_ID, COMPANY_ID, [SlowSink(SLOW_WRITE_SECONDS)])
    elif fault == "F3":
        tracer = EmitBrokenTracer(RUN_ID, COMPANY_ID, [MemorySink()])
    elif fault == "F4":
        tracer = AttemptBrokenTracer(RUN_ID, COMPANY_ID, [MemorySink()])
    elif fault == "F5":
        tracer = RunTracer(RUN_ID, COMPANY_ID, [MemorySink()])
        return tracer, [RaisingHandler(tracer)]
    elif fault == "F6":  # 내부 로그 쓰기 불가: 기준 경로가 폴더가 아니라 파일
        blocker = tmp_path / "not-a-folder"
        blocker.write_text("x", encoding="utf-8")
        tracer = RunTracer(RUN_ID, COMPANY_ID, [TraceOutboxSink(blocker)])
    else:
        raise AssertionError(fault)
    return tracer, [tracer.callback_handler()]
