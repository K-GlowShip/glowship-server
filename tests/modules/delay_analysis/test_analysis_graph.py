import asyncio
import logging
import uuid
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from langgraph.errors import GraphInterrupt, ParentCommand
from langgraph.graph import END
from langgraph.types import Command

from app.modules.delay_analysis.application.agent.fake_runtime import (
    FakeScenario,
    fake_runtime,
    fake_tools,
)
from app.modules.delay_analysis.application.agent.graph import (
    COMPARE,
    COORDINATOR,
    EXPLAIN,
    INVENTORY_IMPACT,
    RESPONSE_OPTIONS,
    UNEXPECTED_ERROR_MESSAGE,
    VALIDATE,
    build_analysis_graph,
    route_after_compare,
    route_after_explain,
    route_after_prepare,
    route_from_coordinator,
    route_to_coordinator,
    with_failure_stop,
)
from app.modules.delay_analysis.application.agent.nodes.compare import (
    option_differences,
)
from app.modules.delay_analysis.application.agent.retry import (
    EXPLANATION_GENERATION_FAILED,
    MISSING_REQUIRED_DATA,
    PERMISSION_DENIED,
    TOOL_TEMPORARY_ERROR_EXHAUSTED,
    UNEXPECTED_TOOL_ERROR,
    PermissionDeniedError,
    TransientToolError,
)
from app.modules.delay_analysis.application.agent.runtime import (
    RUNTIME_KEY,
    RuntimeWiringError,
)
from app.modules.delay_analysis.application.agent.state import (
    AnalysisOutcome,
    AnalysisStep,
)
from app.modules.delay_analysis.application.agent.tools.common import ToolName
from app.modules.delay_analysis.application.agent.tools.option_schemas import (
    OptionOutcomes,
)
from app.modules.delay_analysis.application.agent.tools.registry import ToolSpec
from app.modules.delay_analysis.infrastructure.analysis_enums import (
    CompletedStatus,
    OptionType,
    ResultClass,
)

"""
    지연 분석 그래프 흐름 시험 (GLOW-129, docs/agent-flow/delay-analysis-graph.md)
    가짜 실행 도구 묶음(fake_runtime)으로 종료 갈래마다 하나씩: READY, NO_DELAY, ON_HOLD, NO_RISK, 예측 부족만(READY),
    도구 실패(노드별 실패 단계), 엔진 판정 서명과 다름, 설명 3회 실패, 최종 확인 실패, STALE 경계 4곳, 실행 도구 빠짐,
    도구 밖 함수의 예외 8곳(이슈 #57 1번: FAILED·UNEXPECTED_TOOL_ERROR, 연결 실수·취소는 그대로 올림).
    도구를 부른 순서는 call_tool 의 추적 훅(첫 시도만)으로 본다.
"""

T = ToolName
IMPACT_CALLS = [
    T.GET_DELAY_CONTEXT,
    T.GET_INVENTORY_SNAPSHOT,
    T.GET_INBOUND_SCHEDULE,
    T.GET_SALES_HISTORY,
    T.GET_OPEN_ORDERS,
    T.GET_OPERATING_RULES,
    T.IDENTIFY_AT_RISK_ORDERS,  # T7 이 T6 보다 먼저(계약 Q3)
    T.CALCULATE_INVENTORY_IMPACT,
]
OPTION_CALLS = [
    T.GET_INVENTORY_SNAPSHOT,  # 설계대로 다시 조회(agentflow 5.3)
    T.GET_OPERATING_RULES,
    T.CHECK_OPTION_FEASIBILITY,
    T.CALCULATE_OPTION_OUTCOMES,
]


def _input() -> dict:
    return {
        "analysis_run_id": uuid.uuid4(),
        "company_id": uuid.uuid4(),
        "delay_event_id": uuid.uuid4(),
        "operating_rule_set_id": uuid.uuid4(),
    }


class _Tracer:
    def __init__(self):
        self.records = []

    def tool_attempt(self, record):
        self.records.append(record)

    def first_calls(self) -> list[str]:
        return [r.tool_name for r in self.records if r.attempt == 1]


class FakeRunStatusReader:
    """stale_on_call 번째 조회부터 STALE 을 반환하는 실행 상태 조회기"""

    def __init__(self, stale_on_call: int):
        self.stale_on_call = stale_on_call
        self.calls = 0

    async def get_status(self, analysis_run_id: uuid.UUID) -> str:
        self.calls += 1
        return "STALE" if self.calls >= self.stale_on_call else "ANALYZING_IMPACT"


async def _run(scenario=None, *, tools=None, reader=None, runtime=None):
    tracer = _Tracer()
    runtime = runtime or fake_runtime(scenario, tools=tools, tracer=tracer)
    configurable = {RUNTIME_KEY: runtime}
    if reader is not None:
        configurable["run_status_reader"] = reader
    result = await build_analysis_graph().ainvoke(
        _input(), {"configurable": configurable}
    )
    return result, tracer, runtime


def _replace_tool(name: ToolName, fn) -> dict:
    spec = fake_tools(FakeScenario())[name]
    return {name: replace(spec, fn=fn)}


async def test_정상_흐름은_7개_노드와_도구_14번을_문서_순서대로_거쳐_READY_FOR_REVIEW로_끝난다():
    result, tracer, runtime = await _run()

    assert result["visited_nodes"] == [
        "prepare",
        "coordinator",
        "inventory_impact",
        "coordinator",
        "response_options",
        "coordinator",
        "compare",
        "explain",
        "validate",
    ]
    assert result["outcome"] == AnalysisOutcome.READY_FOR_REVIEW
    assert result["step"] == AnalysisStep.VALIDATING_OUTPUT
    assert tracer.first_calls() == [
        *IMPACT_CALLS,
        *OPTION_CALLS,
        T.COMPARE_OPTIONS,
        T.VALIDATE_AGENT_OUTPUT,
    ]
    assert runtime.preparer.calls == ["judge_no_delay", "build_engine_input"]
    # 서비스가 저장에 넘길 결과가 상태에 남는다(인터페이스 문서 7절)
    for key in (
        "delay_context",
        "inventory_snapshot",
        "sales_history",
        "open_orders",
        "operating_rules",
        "at_risk_orders",
        "inventory_impact",
        "option_outcomes",
        "option_ranking",
        "decision",
        "explanation",
    ):
        assert key in result, key
    assert result["explanation"].generated_at == datetime(2026, 1, 2, 9, 0, tzinfo=UTC)
    assert "failure" not in result


async def test_지연이_해소되면_엔진_입력을_만들지_않고_NO_DELAY로_끝난다():
    result, tracer, runtime = await _run(FakeScenario(no_delay=True))

    assert result["outcome"] == AnalysisOutcome.NO_DELAY
    assert result["visited_nodes"] == ["prepare"]
    assert "no_delay" in result
    assert runtime.preparer.calls == ["judge_no_delay"]  # 엔진 저장소를 읽기 전에 끝남
    assert tracer.first_calls() == []


async def test_엔진_입력이_보류면_ON_HOLD와_VALIDATING_실패_요약으로_끝난다():
    result, tracer, _ = await _run(FakeScenario(hold_code=MISSING_REQUIRED_DATA))

    assert result["outcome"] == AnalysisOutcome.ON_HOLD
    failure = result["failure"]
    assert (failure.status, failure.failed_step, failure.failure_code) == (
        AnalysisOutcome.ON_HOLD,
        AnalysisStep.VALIDATING,
        MISSING_REQUIRED_DATA,
    )
    assert tracer.first_calls() == []


_WARNINGS = (
    {"source_domain": "SHIPMENT", "last_status": "FAILED", "last_finished_at": None,
     "reasons": ["LAST_SYNC_FAILED"], "limit_date": None},
)  # fmt: skip


async def test_엔진_입력이_정한_데이터_경고를_정상과_보류_상태에_싣고_지연_해소에는_없다():
    """이슈 #122: 분석 준비 노드는 값만 나른다(정상·보류). 지연 해소는 엔진 입력을 만들지 않아 키가 없다."""
    ready, _, _ = await _run(FakeScenario(data_warnings=_WARNINGS))
    held, _, _ = await _run(
        FakeScenario(hold_code=MISSING_REQUIRED_DATA, data_warnings=_WARNINGS)
    )
    plain, _, _ = await _run(FakeScenario())
    no_delay, _, _ = await _run(FakeScenario(no_delay=True, data_warnings=_WARNINGS))

    assert ready["data_warnings"] == list(_WARNINGS)
    assert held["outcome"] == AnalysisOutcome.ON_HOLD
    assert held["data_warnings"] == list(_WARNINGS)
    assert plain["data_warnings"] == []
    assert "data_warnings" not in no_delay


async def test_재고_영향이_NO_RISK면_판정_서명을_만들고_대응안_없이_끝난다():
    scenario = FakeScenario(
        completed_status=CompletedStatus.NO_RISK,
        result_class=ResultClass.PURE_NO_RISK,
        has_at_risk_orders=False,
    )
    result, tracer, _ = await _run(scenario)

    assert result["outcome"] == AnalysisOutcome.NO_RISK
    assert result["visited_nodes"][-1] == "coordinator"
    assert tracer.first_calls() == IMPACT_CALLS
    assert "decision" in result  # agentflow 5.5: NO_RISK 도 의존 범위·판정 서명
    assert "option_outcomes" not in result


async def test_위험_주문이_없어도_예측상_부족이면_READY로_대응안을_분석한다():
    scenario = FakeScenario(
        result_class=ResultClass.FORECAST_ONLY_READY, has_at_risk_orders=False
    )
    result, tracer, _ = await _run(scenario)

    assert result["has_at_risk_orders"] is False
    assert result["outcome"] == AnalysisOutcome.READY_FOR_REVIEW
    assert T.CALCULATE_OPTION_OUTCOMES in tracer.first_calls()


async def test_재고_영향_도구가_재시도_뒤에도_실패하면_FAILED로_끝나고_대응안으로_가지_않는다():
    async def flaky(ctx, payload):
        raise TransientToolError("원천 응답 지연")

    result, tracer, _ = await _run(tools=_replace_tool(T.GET_OPEN_ORDERS, flaky))

    assert result["outcome"] == AnalysisOutcome.FAILED
    failure = result["failure"]
    assert (
        failure.failed_step,
        failure.failure_code,
        failure.attempt_count,
        failure.retry_exhausted,
    ) == (AnalysisStep.ANALYZING_IMPACT, TOOL_TEMPORARY_ERROR_EXHAUSTED, 3, True)
    assert result["visited_nodes"][-1] == "inventory_impact:failed"
    assert T.CHECK_OPTION_FEASIBILITY not in tracer.first_calls()


async def test_대응안_단계에서_다시_부른_T2가_실패하면_실패_단계는_ANALYZING_OPTIONS다():
    calls = []

    async def second_call_denied(ctx, payload):
        calls.append(1)
        if len(calls) == 2:  # 첫 번째는 재고 영향 단계, 두 번째가 대응안 단계
            raise PermissionDeniedError("다른 고객사")
        return await fake_tools(FakeScenario())[T.GET_INVENTORY_SNAPSHOT].fn(
            ctx, payload
        )

    result, _, _ = await _run(
        tools=_replace_tool(T.GET_INVENTORY_SNAPSHOT, second_call_denied)
    )

    failure = result["failure"]
    assert (failure.failed_step, failure.failure_code) == (
        AnalysisStep.ANALYZING_OPTIONS,
        PERMISSION_DENIED,
    )
    assert result["visited_nodes"][-1] == "response_options:failed"


async def test_지연_문맥_조회가_실패하면_조정_에이전트에서_FAILED로_끝난다():
    async def broken(ctx, payload):
        raise KeyError("bug")

    result, _, _ = await _run(tools=_replace_tool(T.GET_DELAY_CONTEXT, broken))

    failure = result["failure"]
    assert (failure.failed_step, failure.failure_code) == (
        AnalysisStep.ANALYZING_IMPACT,
        UNEXPECTED_TOOL_ERROR,
    )
    assert result["visited_nodes"][-1] == "coordinator:failed"


def _warned(caplog) -> str:
    """서버 로그 경고(코드 오류의 자세한 사유)를 한 문자열로"""
    return "\n".join(
        r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
    )


async def test_추천이_엔진_판정_서명과_다르면_설명_전에_FAILED로_멈춘다(caplog):
    with caplog.at_level(logging.WARNING):
        result, tracer, _ = await _run(
            FakeScenario(engine_recommended=OptionType.KEEP_PLAN)
        )

    failure = result["failure"]
    assert (failure.failed_step, failure.failure_code) == (
        AnalysisStep.VALIDATING_OUTPUT,
        UNEXPECTED_TOOL_ERROR,
    )
    # Client 문구는 고정, 어디가 다른지는 서버 로그에만(이슈 #57 2번)
    assert failure.failure_message == UNEXPECTED_ERROR_MESSAGE
    assert "추천: 엔진 KEEP_PLAN" in _warned(caplog)
    assert result["visited_nodes"][-1] == "compare:failed"
    assert T.VALIDATE_AGENT_OUTPUT not in tracer.first_calls()
    assert "decision" not in result


async def test_대응안_값이_엔진과_다르면_어느_값인지_서버_로그에_적고_FAILED로_멈춘다(
    caplog,
):
    original = fake_tools(FakeScenario())[T.CALCULATE_OPTION_OUTCOMES].fn

    async def changed(ctx, payload):
        out = await original(ctx, payload)
        options = [
            o.model_copy(update={"reduction_committed_total": 99})
            if o.option_type == OptionType.OTHER_REGION_FULFILL
            else o
            for o in out.options
        ]
        return OptionOutcomes.model_construct(options=options)

    with caplog.at_level(logging.WARNING):
        result, _, _ = await _run(
            tools=_replace_tool(T.CALCULATE_OPTION_OUTCOMES, changed)
        )

    assert result["outcome"] == AnalysisOutcome.FAILED
    assert result["failure"].failure_message == UNEXPECTED_ERROR_MESSAGE
    assert "OTHER_REGION_FULFILL.reduction_committed_total" in _warned(caplog)
    assert str(result["analysis_run_id"]) in _warned(caplog)  # 어느 실행인지


async def test_설명이_3번_실패하면_계산_결과를_남기고_FAILED로_끝난다():
    result, tracer, _ = await _run(FakeScenario(explanation_fails=True))

    failure = result["failure"]
    assert (
        failure.failed_step,
        failure.failure_code,
        failure.attempt_count,
        failure.retry_exhausted,
    ) == (AnalysisStep.VALIDATING_OUTPUT, EXPLANATION_GENERATION_FAILED, 3, True)
    assert result["visited_nodes"][-1] == "explain"
    # agentflow 5.5 "설명 검증 계속 실패": 계산 결과는 서비스가 함께 저장한다
    assert {"option_outcomes", "option_ranking", "decision"} <= set(result)
    assert result["explanation_failed"].prompt_version == "fake@1"
    assert T.VALIDATE_AGENT_OUTPUT not in tracer.first_calls()


async def test_최종_확인에서_떨어지면_연결_오류로_FAILED가_된다(caplog):
    with caplog.at_level(logging.WARNING):
        result, _, _ = await _run(FakeScenario(final_check_passes=False))

    failure = result["failure"]
    assert (failure.failed_step, failure.failure_code) == (
        AnalysisStep.VALIDATING_OUTPUT,
        UNEXPECTED_TOOL_ERROR,
    )
    assert failure.failure_message == UNEXPECTED_ERROR_MESSAGE
    assert "NUMBER_MISMATCH@risk_summary#s1" in _warned(caplog)
    assert result["visited_nodes"][-1] == "validate:failed"


@pytest.mark.parametrize(
    ("stale_on_call", "stopped_node", "stopped_at", "last_completed"),
    [
        (1, "coordinator", AnalysisStep.VALIDATING, "prepare"),  # 분석 준비 뒤
        (2, "coordinator", AnalysisStep.ANALYZING_IMPACT, "inventory_impact"),
        (3, "coordinator", AnalysisStep.ANALYZING_OPTIONS, "response_options"),
        (4, "explain", AnalysisStep.VALIDATING_OUTPUT, "compare"),  # 비교 뒤
    ],
)
async def test_노드_경계에서_STALE이면_남은_단계를_진행하지_않는다(
    stale_on_call, stopped_node, stopped_at, last_completed
):
    result, _, _ = await _run(reader=FakeRunStatusReader(stale_on_call))

    assert result["outcome"] == AnalysisOutcome.STALE
    assert result["stopped_at"] == stopped_at
    assert result["visited_nodes"][-2:] == [
        last_completed,
        f"{stopped_node}:stale_stop",
    ]
    assert "validate" not in result["visited_nodes"]


async def test_조회기가_없으면_STALE_확인을_건너뛴다():
    result, _, _ = await _run()

    assert result["outcome"] == AnalysisOutcome.READY_FOR_REVIEW


async def test_실행_도구_묶음이_없으면_빈_결과로_넘어가지_않고_오류를_낸다():
    with pytest.raises(RuntimeError, match="analysis_runtime"):
        await build_analysis_graph().ainvoke(_input(), {"configurable": {}})


async def test_실행_도구_묶음은_실행_하나에만_쓴다():
    runtime = fake_runtime()
    await _run(runtime=runtime)

    with pytest.raises(RuntimeError, match="실행 하나"):
        await _run(runtime=runtime)


def test_도구가_하나라도_빠진_묶음은_만들_때_막는다():
    runtime = fake_runtime()
    tools = dict(runtime.registry)
    del tools[T.COMPARE_OPTIONS]

    with pytest.raises(ValueError, match="compare_options"):
        replace(runtime, registry=tools)


async def test_엔진과_같은_대응안_결과는_차이가_없다():
    tools = fake_tools(FakeScenario())
    outcomes = await tools[T.CALCULATE_OPTION_OUTCOMES].fn(None, None)
    ranking = await tools[T.COMPARE_OPTIONS].fn(None, None)

    analysis = fake_runtime().preparer.engine.analysis
    assert option_differences(outcomes, ranking, analysis) == []


@pytest.mark.parametrize("outcome", [AnalysisOutcome.NO_DELAY, AnalysisOutcome.ON_HOLD])
def test_분석_준비에서_종료_상태가_정해지면_바로_끝난다(outcome):
    assert route_after_prepare({"outcome": outcome}) == END


def test_분석_준비를_통과하면_조정_에이전트로_간다():
    assert route_after_prepare({}) == COORDINATOR


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({}, INVENTORY_IMPACT),
        ({"inventory_impact": object()}, RESPONSE_OPTIONS),
        ({"inventory_impact": object(), "option_outcomes": object()}, COMPARE),
        ({"outcome": AnalysisOutcome.NO_RISK}, END),
        ({"outcome": AnalysisOutcome.STALE}, END),
        ({"outcome": AnalysisOutcome.FAILED}, END),
    ],
)
def test_조정_에이전트는_고정된_순서로_다음_분석을_고른다(state, expected):
    assert route_from_coordinator(state) == expected


def test_하위_에이전트_비교_설명_뒤에는_종료_상태가_있으면_끝난다():
    assert route_to_coordinator({}) == COORDINATOR
    assert route_to_coordinator({"outcome": AnalysisOutcome.FAILED}) == END
    assert route_after_compare({}) == EXPLAIN
    assert route_after_compare({"outcome": AnalysisOutcome.FAILED}) == END
    assert route_after_explain({}) == VALIDATE
    assert route_after_explain({"outcome": AnalysisOutcome.STALE}) == END


def test_가짜_도구는_도구_12개를_모두_등록한다():
    assert set(fake_tools(FakeScenario())) == set(ToolName)
    assert all(isinstance(s, ToolSpec) for s in fake_tools(FakeScenario()).values())


# --- 도구 밖 함수의 예외 (이슈 #57 1번) ---------------------------------------------------
SECRET = "db down A-ORD-20260613-0040"  # 예외 문구는 Client 문구(failure_message)에 들어가면 안 됨

NO_RISK = FakeScenario(
    completed_status=CompletedStatus.NO_RISK,
    result_class=ResultClass.PURE_NO_RISK,
    has_at_risk_orders=False,
)


def _raise(error: BaseException):
    def fail(*_args, **_kwargs):
        raise error

    return fail


async def _raise_async_on(method: str, runtime, error: BaseException):
    """runtime.preparer 의 한 메서드만 예외를 내게 바꾼다."""
    preparer = runtime.preparer
    original = getattr(preparer, method)

    async def broken():
        await original()
        raise error

    object.__setattr__(preparer, method, broken)
    return runtime


class _BrokenReader:
    """fail_on_call 번째 실행 상태 조회에서 예외(DB 장애 흉내)"""

    def __init__(self, fail_on_call: int):
        self.fail_on_call = fail_on_call
        self.calls = 0

    async def get_status(self, analysis_run_id: uuid.UUID) -> str:
        self.calls += 1
        if self.calls >= self.fail_on_call:
            raise ConnectionError(SECRET)
        return "ANALYZING_IMPACT"


def _broken_explain(runtime, error: BaseException):
    async def explain(ctx, now):
        raise error

    return replace(runtime, explain=explain)


async def _broken_runtime(where: str, error: BaseException):
    if where == "judge_no_delay":
        return None, await _raise_async_on("judge_no_delay", fake_runtime(), error)
    if where == "build_engine_input":
        return None, await _raise_async_on("build_engine_input", fake_runtime(), error)
    if where == "decide@no_risk":
        return None, replace(fake_runtime(NO_RISK), decide=_raise(error))
    if where == "decide@compare":
        return None, replace(fake_runtime(), decide=_raise(error))
    if where == "explain":
        return None, _broken_explain(fake_runtime(), error)
    if where == "final_check_input":
        return None, replace(fake_runtime(), final_check_input=_raise(error))
    if where == "stale@coordinator":
        return _BrokenReader(1), fake_runtime()
    if where == "stale@explain":
        return _BrokenReader(4), fake_runtime()
    raise AssertionError(where)


@pytest.mark.parametrize(
    ("where", "node", "failed_step", "kept"),
    [
        ("judge_no_delay", "prepare", AnalysisStep.VALIDATING, None),
        ("build_engine_input", "prepare", AnalysisStep.VALIDATING, None),
        ("stale@coordinator", COORDINATOR, AnalysisStep.ANALYZING_IMPACT, None),
        (
            "decide@no_risk",
            COORDINATOR,
            AnalysisStep.ANALYZING_IMPACT,
            "inventory_impact",
        ),
        ("decide@compare", COMPARE, AnalysisStep.VALIDATING_OUTPUT, "option_outcomes"),
        ("stale@explain", EXPLAIN, AnalysisStep.VALIDATING_OUTPUT, "decision"),
        ("explain", EXPLAIN, AnalysisStep.VALIDATING_OUTPUT, "decision"),
        ("final_check_input", VALIDATE, AnalysisStep.VALIDATING_OUTPUT, "explanation"),
    ],
)
async def test_도구_밖_함수가_예외를_내면_그_노드에서_FAILED_실패_요약으로_끝난다(
    where, node, failed_step, kept, caplog
):
    reader, runtime = await _broken_runtime(where, ConnectionError(SECRET))

    with caplog.at_level(logging.ERROR):
        result, _, _ = await _run(runtime=runtime, reader=reader)

    assert result["outcome"] == AnalysisOutcome.FAILED
    failure = result["failure"]
    assert (
        failure.status,
        failure.failed_step,
        failure.failure_code,
        failure.attempt_count,
        failure.retry_exhausted,
    ) == (AnalysisOutcome.FAILED, failed_step, UNEXPECTED_TOOL_ERROR, 1, False)
    assert failure.failure_message == UNEXPECTED_ERROR_MESSAGE  # 고정 문구
    assert SECRET not in failure.failure_message
    assert result["step"] == failed_step
    assert result["visited_nodes"][-1] == f"{node}:failed"
    if kept:  # 그때까지 채운 결과는 남아 서비스가 함께 저장할 수 있음
        assert kept in result
    if node != VALIDATE:
        assert "validate" not in result["visited_nodes"]
    # 예외 내용은 서버 로그로(노드 이름·실행 ID·원래 예외)
    logged = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(logged) == 1
    assert node in logged[0].getMessage()
    assert isinstance(logged[0].exc_info[1], ConnectionError)


async def test_도구_밖_예외로_끝난_실패_요약은_저장_열_5개를_채운다():
    reader, runtime = await _broken_runtime("explain", ValueError("bug"))
    result, _, _ = await _run(runtime=runtime, reader=reader)

    columns = result["failure"].columns()
    assert columns["status"] == "FAILED"
    assert columns["failed_step"] == "VALIDATING_OUTPUT"
    assert columns["failure_code"] == UNEXPECTED_TOOL_ERROR
    assert columns["attempt_count"] == 1
    assert columns["retry_exhausted"] is False


async def test_밖에서_실행을_취소하면_실패_요약으로_삼키지_않고_취소된다():
    started = asyncio.Event()

    async def explain(
        ctx, now
    ):  # 설명 생성 중에 서비스가 실행을 취소함(종료·시간 예산)
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(_run(runtime=replace(fake_runtime(), explain=explain)))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize(
    "signal",
    [GraphInterrupt(), ParentCommand(Command(goto=END))],
    ids=["GraphInterrupt", "ParentCommand"],
)
async def test_LangGraph_제어_신호는_실패_요약으로_바꾸지_않고_올린다(signal):
    # GraphBubbleUp(interrupt·Command 등)은 Exception 하위지만 LangGraph 가 받아야 하는 신호다
    async def node(state, config):
        raise signal

    wrapped = with_failure_stop(EXPLAIN, node, AnalysisStep.VALIDATING_OUTPUT)

    with pytest.raises(type(signal)):
        await wrapped(_input(), {})


async def test_실행_도구_묶음_연결_실수는_실패_요약으로_바꾸지_않고_올린다():
    with pytest.raises(RuntimeWiringError, match="AnalysisRuntime 이어야"):
        await build_analysis_graph().ainvoke(
            _input(), {"configurable": {RUNTIME_KEY: object()}}
        )
    with pytest.raises(RuntimeWiringError, match="analysis_runtime"):
        await build_analysis_graph().ainvoke(_input(), {"configurable": {}})


async def test_tool_context_takes_base_date_from_engine_input():
    """기준일 D 는 엔진 입력(EngineInput.base_date, #67)에서 온다. 그날 자료가 없으면 실행 날짜보다 이르다."""
    import dataclasses
    from datetime import date

    runtime = fake_runtime()
    pulled = dataclasses.replace(runtime.preparer.engine, base_date=date(2025, 12, 31))
    runtime.preparer.engine = pulled
    seen = []
    t1 = runtime.registry[ToolName.GET_DELAY_CONTEXT]

    async def spy(ctx, payload):
        seen.append(payload.base_date)
        return await t1.fn(ctx, payload)

    runtime.registry[ToolName.GET_DELAY_CONTEXT] = dataclasses.replace(t1, fn=spy)
    await _run(runtime=runtime)

    assert runtime.ctx.base_date == date(2025, 12, 31)
    assert seen == [date(2025, 12, 31)]
