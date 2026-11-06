from langchain_core.runnables import RunnableConfig

from app.modules.delay_analysis.application.agent.runtime import runtime_of
from app.modules.delay_analysis.application.agent.state import (
    AnalysisState,
    AnalysisStep,
)
from app.modules.delay_analysis.application.agent.tools.common import ToolName
from app.modules.delay_analysis.application.agent.tools.impact_schemas import (
    CalculateInventoryImpactInput,
    IdentifyAtRiskOrdersInput,
)


async def inventory_impact_node(state: AnalysisState, config: RunnableConfig) -> dict:
    """
    재고 영향 분석 에이전트 (agentflow 5.2, 계약 9절 Q3)
      - 조회 T2 가용 재고 → T3 입고 예정 → T4 최근 28일 수요 → T5 미출고 주문 → T8 운영 기준(입력 없음, 결정 3)
      - 계산 T7 위험 주문 → T6 재고 영향. T6 이 T7 의 지연 유발 SKU 를 쓰므로 T7 이 먼저다(계약 Q3).
      - 실패 단계는 ANALYZING_IMPACT. 도구가 멈추면 call_tool 이 ToolFailure 를 올리고 그래프가 FAILED 로 끝낸다.
      - 결과는 조정 에이전트로 돌아갑니다.
    """
    runtime = runtime_of(config)
    step = AnalysisStep.ANALYZING_IMPACT

    snapshot = await runtime.call(ToolName.GET_INVENTORY_SNAPSHOT, None, step=step)
    inbound = await runtime.call(ToolName.GET_INBOUND_SCHEDULE, None, step=step)
    sales = await runtime.call(ToolName.GET_SALES_HISTORY, None, step=step)
    orders = await runtime.call(ToolName.GET_OPEN_ORDERS, None, step=step)
    rules = await runtime.call(ToolName.GET_OPERATING_RULES, None, step=step)

    at_risk = await runtime.call(
        ToolName.IDENTIFY_AT_RISK_ORDERS,
        IdentifyAtRiskOrdersInput(
            delay_context=state["delay_context"],
            inventory_snapshot=snapshot,
            inbound_schedule=inbound,
            open_orders=orders,
            operating_rules=rules,
        ),
        step=step,
    )
    impact = await runtime.call(
        ToolName.CALCULATE_INVENTORY_IMPACT,
        CalculateInventoryImpactInput(
            delay_context=state["delay_context"],
            inventory_snapshot=snapshot,
            inbound_schedule=inbound,
            sales_history=sales,
            open_orders=orders,
            operating_rules=rules,
            delay_induced_sku_codes=at_risk.delay_induced_sku_codes,
        ),
        step=step,
    )
    return {
        "inventory_snapshot": snapshot,
        "inbound_schedule": inbound,
        "sales_history": sales,
        "open_orders": orders,
        "operating_rules": rules,
        "at_risk_orders": at_risk,
        "inventory_impact": impact,
        "has_at_risk_orders": bool(at_risk.risks),
        "visited_nodes": ["inventory_impact"],
    }
