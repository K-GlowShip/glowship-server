import uuid
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from langgraph.graph import END

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
    VALIDATE,
    build_analysis_graph,
    route_after_compare,
    route_after_explain,
    route_after_prepare,
    route_from_coordinator,
    route_to_coordinator,
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
from app.modules.delay_analysis.application.agent.runtime import RUNTIME_KEY
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
    도구 실패(노드별 실패 단계), 엔진 판정 서명과 다름, 설명 3회 실패, 최종 확인 실패, STALE 경계 4곳, 실행 도구 빠짐.
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


async def test_추천이_엔진_판정_서명과_다르면_설명_전에_FAILED로_멈춘다():
    result, tracer, _ = await _run(
        FakeScenario(engine_recommended=OptionType.KEEP_PLAN)
    )

    failure = result["failure"]
    assert (failure.failed_step, failure.failure_code) == (
        AnalysisStep.VALIDATING_OUTPUT,
        UNEXPECTED_TOOL_ERROR,
    )
    assert "추천" in failure.failure_message
    assert result["visited_nodes"][-1] == "compare:failed"
    assert T.VALIDATE_AGENT_OUTPUT not in tracer.first_calls()
    assert "decision" not in result


async def test_대응안_값이_엔진과_다르면_어느_값인지_적고_FAILED로_멈춘다():
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

    result, _, _ = await _run(tools=_replace_tool(T.CALCULATE_OPTION_OUTCOMES, changed))

    assert result["outcome"] == AnalysisOutcome.FAILED
    assert (
        "OTHER_REGION_FULFILL.reduction_committed_total"
        in result["failure"].failure_message
    )


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


async def test_최종_확인에서_떨어지면_연결_오류로_FAILED가_된다():
    result, _, _ = await _run(FakeScenario(final_check_passes=False))

    failure = result["failure"]
    assert (failure.failed_step, failure.failure_code) == (
        AnalysisStep.VALIDATING_OUTPUT,
        UNEXPECTED_TOOL_ERROR,
    )
    assert "NUMBER_MISMATCH@risk_summary#s1" in failure.failure_message
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
