import uuid

import pytest
from langgraph.graph import END

from app.modules.delay_analysis.application.agent.graph import (
    COMPARE,
    COORDINATOR,
    INVENTORY_IMPACT,
    RESPONSE_OPTIONS,
    VALIDATE,
    build_analysis_graph,
    route_after_explain,
    route_after_prepare,
    route_from_coordinator,
)
from app.modules.delay_analysis.application.agent.nodes.coordinator import (
    coordinator_node,
)
from app.modules.delay_analysis.application.agent.state import (
    AnalysisOutcome,
    AnalysisStep,
)


def _input() -> dict:
    return {
        "analysis_run_id": uuid.uuid4(),
        "company_id": uuid.uuid4(),
        "delay_event_id": uuid.uuid4(),
        "operating_rule_set_id": uuid.uuid4(),
    }


class FakeRunStatusReader:
    """stale_on_call 번째 조회부터 STALE 을 반환하는 실행 상태 조회기"""

    def __init__(self, stale_on_call: int):
        self.stale_on_call = stale_on_call
        self.calls = 0

    async def get_status(self, analysis_run_id: uuid.UUID) -> str:
        self.calls += 1
        return "STALE" if self.calls >= self.stale_on_call else "ANALYZING_IMPACT"


async def test_정상_흐름은_7개_노드를_문서_순서대로_거쳐_READY_FOR_REVIEW로_끝난다():
    result = await build_analysis_graph().ainvoke(_input())

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


@pytest.mark.parametrize(
    ("stale_on_call", "stopped_node", "stopped_at", "last_completed"),
    [
        (1, "coordinator", AnalysisStep.VALIDATING, "prepare"),  # 분석 준비 뒤
        (2, "coordinator", AnalysisStep.ANALYZING_IMPACT, "inventory_impact"),
        (3, "coordinator", AnalysisStep.ANALYZING_OPTIONS, "response_options"),
        (4, "explain", AnalysisStep.ANALYZING_OPTIONS, "compare"),  # 비교 뒤
    ],
)
async def test_노드_경계에서_STALE이면_남은_단계를_진행하지_않는다(
    stale_on_call, stopped_node, stopped_at, last_completed
):
    reader = FakeRunStatusReader(stale_on_call)

    result = await build_analysis_graph().ainvoke(
        _input(), {"configurable": {"run_status_reader": reader}}
    )

    assert result["outcome"] == AnalysisOutcome.STALE
    assert result["stopped_at"] == stopped_at
    assert result["visited_nodes"][-2:] == [
        last_completed,
        f"{stopped_node}:stale_stop",
    ]
    assert "validate" not in result["visited_nodes"]


async def test_조회기가_없으면_STALE_확인을_건너뛴다():
    result = await build_analysis_graph().ainvoke(_input(), {"configurable": {}})

    assert result["outcome"] == AnalysisOutcome.READY_FOR_REVIEW


@pytest.mark.parametrize("outcome", [AnalysisOutcome.NO_DELAY, AnalysisOutcome.ON_HOLD])
def test_분석_준비에서_종료_상태가_정해지면_바로_끝난다(outcome):
    assert route_after_prepare({"outcome": outcome}) == END


def test_분석_준비를_통과하면_조정_에이전트로_간다():
    assert route_after_prepare({}) == COORDINATOR


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({}, INVENTORY_IMPACT),
        ({"impact_result": {}, "has_at_risk_orders": True}, RESPONSE_OPTIONS),
        (
            {"impact_result": {}, "has_at_risk_orders": True, "option_results": {}},
            COMPARE,
        ),
        ({"outcome": AnalysisOutcome.NO_RISK}, END),
        ({"outcome": AnalysisOutcome.STALE}, END),
    ],
)
def test_조정_에이전트는_고정된_순서로_다음_분석을_고른다(state, expected):
    assert route_from_coordinator(state) == expected


async def test_위험_주문이_없으면_조정_에이전트가_NO_RISK로_종료한다():
    update = await coordinator_node({"impact_result": {}, "has_at_risk_orders": False})

    assert update["outcome"] == AnalysisOutcome.NO_RISK
    assert route_from_coordinator(update) == END


def test_설명_뒤에는_종료_상태가_없으면_결과_검증으로_간다():
    assert route_after_explain({}) == VALIDATE
    assert route_after_explain({"outcome": AnalysisOutcome.STALE}) == END
