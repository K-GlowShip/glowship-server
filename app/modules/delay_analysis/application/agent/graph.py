import logging
from collections.abc import Awaitable, Callable

from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphBubbleUp
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
from app.modules.delay_analysis.application.agent.retry import (
    UNEXPECTED_ERROR_MESSAGE,
    UNEXPECTED_TOOL_ERROR,
    FailureSummary,
    ToolFailure,
)
from app.modules.delay_analysis.application.agent.run_status import is_run_stale
from app.modules.delay_analysis.application.agent.runtime import RuntimeWiringError
from app.modules.delay_analysis.application.agent.state import (
    AnalysisOutcome,
    AnalysisState,
    AnalysisStep,
)
from app.modules.delay_analysis.infrastructure.analysis_enums import CompletedStatus

"""
    지연 분석 LangGraph 조립 (agentflow_glowship.md 2·5절)

    분석 준비 ─┬─ (NO_DELAY·ON_HOLD) ──────────────────────────────▶ 종료
               └▶ 조정 에이전트 ⇄ 재고 영향 분석 에이전트  (위험 주문 없음 → NO_RISK 종료)
                       ⇄ 대응안 분석 에이전트
                       └▶ 대응안 비교 ▶ 추천 근거 설명 ▶ 결과 검증 ▶ 종료(READY_FOR_REVIEW)

    STALE 확인 경계: 분석 준비 뒤·재고 영향 뒤·대응안 분석 뒤(= 조정 에이전트 진입), 비교 뒤(= 설명 진입)
    도구 실패(ToolFailure): 도구를 부르는 노드에서 나면 FAILED 와 실패 요약을 남기고 종료(agentflow 5.4·7.1)
    도구 밖 함수의 예외(엔진 입력·판정 서명·설명·최종 확인 입력·STALE 조회 등): 모든 노드에서 같은 방식으로
      FAILED·UNEXPECTED_TOOL_ERROR 실패 요약으로 바꿔 종료(이슈 #57 1번). 실행 도구 묶음 연결 실수
      (RuntimeWiringError)와 LangGraph 제어 신호(GraphBubbleUp)만 그대로 올린다.
    실행 도구(등록표·엔진 입력 등)는 config["configurable"]["analysis_runtime"] (runtime.py)
"""

logger = logging.getLogger(__name__)

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


def unexpected_error_summary(step: AnalysisStep) -> FailureSummary:
    """
    도구 밖 함수의 예외 → 실패 요약(agentflow 7.1 "분류되지 않은 오류").
    도구 밖 함수는 재시도하지 않으므로(재시도는 도구 단위) 시도 1, 소진 아님.
    """
    return FailureSummary(
        status=AnalysisOutcome.FAILED,
        failed_step=step,
        failure_code=UNEXPECTED_TOOL_ERROR,
        attempt_count=1,
        retry_exhausted=False,
        failure_message=UNEXPECTED_ERROR_MESSAGE,
    )


def coordinator_step(state: AnalysisState) -> AnalysisStep:
    """
    조정 에이전트가 이번에 맡는 단계(coordinator_node 와 같은 규칙): 재고 영향 결과 전(T1 조회)과
    NO_RISK 판정까지는 ANALYZING_IMPACT, 그 뒤(대응안 분석·비교로 보냄)는 ANALYZING_OPTIONS.
    """
    impact = state.get("inventory_impact")
    if impact is None or impact.completed_status == CompletedStatus.NO_RISK:
        return AnalysisStep.ANALYZING_IMPACT
    return AnalysisStep.ANALYZING_OPTIONS


StepOf = AnalysisStep | Callable[[AnalysisState], AnalysisStep]


def with_failure_stop(name: str, node: Node, step: StepOf) -> Node:
    """
    노드가 멈추면 FAILED 와 실패 요약을 남기고 종료로 보낸다. 그 전까지 채운 결과는 상태에 그대로 남는다.
      - 도구가 멈춤(call_tool 의 ToolFailure): 실패 요약 그대로. 실패 단계는 부르는 노드가 정한 failed_step.
        코드 오류(code_error)의 자세한 사유(detail)는 서버 로그 경고로 남긴다.
      - 그 밖의 예외(Exception): FAILED·UNEXPECTED_TOOL_ERROR·시도 1. 실패 단계는 step
        (노드마다 고정, 조정 에이전트는 들어올 때의 상태로 정함).
      - 실행 도구 묶음 연결 실수(RuntimeWiringError)는 바꾸지 않고 올린다(부르는 코드의 버그).
      - LangGraph 제어 신호(GraphBubbleUp: interrupt·Command 등)는 Exception 하위지만 오류가 아니라
        LangGraph 가 받아 처리해야 하므로 그대로 올린다(지금은 쓰지 않지만 삼키면 그 기능이 조용히 실패로 바뀜).
      - 작업 취소(asyncio.CancelledError)는 Exception 이 아니라 그대로 올라간다.
    """

    async def stopped(state: AnalysisState, config: RunnableConfig) -> dict:
        try:
            return await node(state, config)
        except ToolFailure as failure:
            summary = failure.summary
            if failure.detail:  # 코드 오류의 자세한 사유는 서버 로그로만(Client 문구는 고정, 이슈 #57 2번)
                logger.warning(
                    "분석 그래프 %s 노드에서 %s 로 FAILED (실행 %s): %s",
                    name,
                    summary.failure_code,
                    state.get("analysis_run_id"),
                    failure.detail,
                )
        except RuntimeWiringError, GraphBubbleUp:
            raise
        except Exception:
            logger.exception(
                "분석 그래프 %s 노드에서 도구 밖 예외로 FAILED (실행 %s)",
                name,
                state.get("analysis_run_id"),
            )
            summary = unexpected_error_summary(
                step if isinstance(step, AnalysisStep) else step(state)
            )
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

    # STALE 확인을 실패 처리 안쪽에 둔다: 실행 상태 조회(DB)가 예외를 내도 FAILED 로 끝난다.
    graph.add_node(
        PREPARE, with_failure_stop(PREPARE, prepare_node, AnalysisStep.VALIDATING)
    )
    graph.add_node(
        COORDINATOR,
        with_failure_stop(
            COORDINATOR,
            with_stale_check(COORDINATOR, coordinator_node),
            coordinator_step,
        ),
    )
    graph.add_node(
        INVENTORY_IMPACT,
        with_failure_stop(
            INVENTORY_IMPACT, inventory_impact_node, AnalysisStep.ANALYZING_IMPACT
        ),
    )
    graph.add_node(
        RESPONSE_OPTIONS,
        with_failure_stop(
            RESPONSE_OPTIONS, response_options_node, AnalysisStep.ANALYZING_OPTIONS
        ),
    )
    graph.add_node(
        COMPARE,
        with_failure_stop(COMPARE, compare_node, AnalysisStep.VALIDATING_OUTPUT),
    )
    graph.add_node(
        EXPLAIN,
        with_failure_stop(
            EXPLAIN,
            with_stale_check(EXPLAIN, explain_node),
            AnalysisStep.VALIDATING_OUTPUT,
        ),
    )
    graph.add_node(
        VALIDATE,
        with_failure_stop(VALIDATE, validate_node, AnalysisStep.VALIDATING_OUTPUT),
    )

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
