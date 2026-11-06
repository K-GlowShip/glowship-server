from collections.abc import Awaitable, Callable

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.modules.delay_analysis.application.agent.nodes.compare import compare_node
from app.modules.delay_analysis.application.agent.nodes.coordinator import (
    coordinator_node,
)
from app.modules.delay_analysis.application.agent.nodes.explain import explain_node
from app.modules.delay_analysis.application.agent.nodes.inventory_impact import (
    inventory_impact_node,
)
from app.modules.delay_analysis.application.agent.nodes.prepare import prepare_node
from app.modules.delay_analysis.application.agent.nodes.response_options import (
    response_options_node,
)
from app.modules.delay_analysis.application.agent.nodes.validate import validate_node
from app.modules.delay_analysis.application.agent.retry import ToolFailure
from app.modules.delay_analysis.application.agent.run_status import is_run_stale
from app.modules.delay_analysis.application.agent.state import (
    AnalysisOutcome,
    AnalysisState,
)

"""
    지연 분석 LangGraph 조립 (agentflow_glowship.md 2·5절)

    분석 준비 ─┬─ (NO_DELAY·ON_HOLD) ──────────────────────────────▶ 종료
               └▶ 조정 에이전트 ⇄ 재고 영향 분석 에이전트  (위험 주문 없음 → NO_RISK 종료)
                       ⇄ 대응안 분석 에이전트
                       └▶ 대응안 비교 ▶ 추천 근거 설명 ▶ 결과 검증 ▶ 종료(READY_FOR_REVIEW)

    STALE 확인 경계: 분석 준비 뒤·재고 영향 뒤·대응안 분석 뒤(= 조정 에이전트 진입), 비교 뒤(= 설명 진입)
    도구 실패(ToolFailure): 도구를 부르는 노드에서 나면 FAILED 와 실패 요약을 남기고 종료(agentflow 5.4·7.1)
    실행 도구(등록표·엔진 입력 등)는 config["configurable"]["analysis_runtime"] (runtime.py)
"""

PREPARE = "prepare"
COORDINATOR = "coordinator"
INVENTORY_IMPACT = "inventory_impact"
RESPONSE_OPTIONS = "response_options"
COMPARE = "compare"
EXPLAIN = "explain"
VALIDATE = "validate"

Node = Callable[[AnalysisState, RunnableConfig], Awaitable[dict]]


def with_stale_check(name: str, node: Node) -> Node:
    """노드 진입 전에 실행 상태를 확인해 STALE 이면 노드를 실행하지 않고 중단 상태를 남긴다."""

    async def guarded(state: AnalysisState, config: RunnableConfig) -> dict:
        if await is_run_stale(config, state["analysis_run_id"]):
            return {
                "outcome": AnalysisOutcome.STALE,
                "stopped_at": state.get("step"),
                "visited_nodes": [f"{name}:stale_stop"],
            }
        return await node(state, config)

    return guarded


def with_failure_stop(name: str, node: Node) -> Node:
    """
    노드 안에서 도구가 멈추면(call_tool 의 ToolFailure) FAILED 와 실패 요약을 남기고 종료로 보낸다.
    실패 단계는 실패 요약의 failed_step(부르는 노드가 정함)이다. 그 전까지 채운 결과는 상태에 그대로 남는다.
    """

    async def stopped(state: AnalysisState, config: RunnableConfig) -> dict:
        try:
            return await node(state, config)
        except ToolFailure as failure:
            summary = failure.summary
            return {
                "outcome": summary.status,
                "failure": summary,
                "step": summary.failed_step,
                "visited_nodes": [f"{name}:failed"],
            }

    return stopped


def route_after_prepare(state: AnalysisState) -> str:
    return END if "outcome" in state else COORDINATOR


def route_from_coordinator(state: AnalysisState) -> str:
    """조정 에이전트가 고정된 큰 순서 안에서 다음 분석을 고른다."""
    if "outcome" in state:  # NO_RISK·STALE·FAILED
        return END
    if "inventory_impact" not in state:
        return INVENTORY_IMPACT
    if "option_outcomes" not in state:
        return RESPONSE_OPTIONS
    return COMPARE


def route_to_coordinator(state: AnalysisState) -> str:
    """하위 에이전트 뒤: 도구 실패로 끝났으면 종료, 아니면 결과를 조정 에이전트로 돌려준다."""
    return END if "outcome" in state else COORDINATOR


def route_after_compare(state: AnalysisState) -> str:
    return END if "outcome" in state else EXPLAIN


def route_after_explain(state: AnalysisState) -> str:
    return END if "outcome" in state else VALIDATE


def build_analysis_graph() -> CompiledStateGraph:
    graph = StateGraph(AnalysisState)

    graph.add_node(PREPARE, prepare_node)
    graph.add_node(
        COORDINATOR,
        with_stale_check(COORDINATOR, with_failure_stop(COORDINATOR, coordinator_node)),
    )
    graph.add_node(
        INVENTORY_IMPACT, with_failure_stop(INVENTORY_IMPACT, inventory_impact_node)
    )
    graph.add_node(
        RESPONSE_OPTIONS, with_failure_stop(RESPONSE_OPTIONS, response_options_node)
    )
    graph.add_node(COMPARE, with_failure_stop(COMPARE, compare_node))
    graph.add_node(EXPLAIN, with_stale_check(EXPLAIN, explain_node))
    graph.add_node(VALIDATE, with_failure_stop(VALIDATE, validate_node))

    graph.add_edge(START, PREPARE)
    graph.add_conditional_edges(PREPARE, route_after_prepare, [COORDINATOR, END])
    graph.add_conditional_edges(
        COORDINATOR,
        route_from_coordinator,
        [INVENTORY_IMPACT, RESPONSE_OPTIONS, COMPARE, END],
    )
    # 하위 에이전트는 결과를 조정 에이전트로 돌려준다(도구 실패면 종료).
    graph.add_conditional_edges(
        INVENTORY_IMPACT, route_to_coordinator, [COORDINATOR, END]
    )
    graph.add_conditional_edges(
        RESPONSE_OPTIONS, route_to_coordinator, [COORDINATOR, END]
    )
    graph.add_conditional_edges(COMPARE, route_after_compare, [EXPLAIN, END])
    graph.add_conditional_edges(EXPLAIN, route_after_explain, [VALIDATE, END])
    graph.add_edge(VALIDATE, END)

    return graph.compile()
