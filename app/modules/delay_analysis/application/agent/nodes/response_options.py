from langchain_core.runnables import RunnableConfig

from app.modules.delay_analysis.application.agent.runtime import runtime_of
from app.modules.delay_analysis.application.agent.state import (
    AnalysisState,
    AnalysisStep,
)
from app.modules.delay_analysis.application.agent.tools.common import ToolName
from app.modules.delay_analysis.application.agent.tools.option_schemas import (
    CalculateOptionOutcomesInput,
    CheckOptionFeasibilityInput,
)


async def response_options_node(state: AnalysisState, config: RunnableConfig) -> dict:
    """
    대응안 분석 에이전트 (agentflow 5.3)
      - 설계대로 T2 가용 재고·T8 운영 기준을 다시 조회한 뒤 T9 후보·사전 판정 → T10 대응안 결과를 계산합니다.
        두 조회는 입력이 없어(결정 3) 재고 영향 단계와 같은 값이 나옵니다. 재사용 안은
        재사용은 개선안으로만 검토(사용자 결정 11-06: 지금은 설계대로).
      - T9 입력 출처는 계약 5절 입력 표(T1·T8·T6·T2·T3), T10 입력은 계약 6절(T9·T1~T5·T8·T6·T7).
      - 실패 단계는 ANALYZING_OPTIONS. 결과는 조정 에이전트로 돌아갑니다.
    """
    runtime = runtime_of(config)
    step = AnalysisStep.ANALYZING_OPTIONS
    delay = state["delay_context"]
    impact = state["inventory_impact"]

    snapshot = await runtime.call(ToolName.GET_INVENTORY_SNAPSHOT, None, step=step)
    rules = await runtime.call(ToolName.GET_OPERATING_RULES, None, step=step)

    feasibility = await runtime.call(
        ToolName.CHECK_OPTION_FEASIBILITY,
        CheckOptionFeasibilityInput(
            transport_mode=delay.transport_mode,
            stage=delay.stage,
            base_date=delay.base_date,
            expected_3pl_inbound_date=delay.expected_3pl_inbound_date,
            planned_kr_release_date=delay.planned_kr_release_date,
            lead_times=rules.lead_times,
            impacted_sku_codes=impact.impacted_sku_codes,
            skus=rules.skus,
            kr_on_hand=snapshot.kr_on_hand,
            donor_next_inbound=state["inbound_schedule"].donor_next_inbound,
        ),
        step=step,
    )
    outcomes = await runtime.call(
        ToolName.CALCULATE_OPTION_OUTCOMES,
        CalculateOptionOutcomesInput(
            option_feasibility=feasibility,
            delay_context=delay,
            inventory_snapshot=snapshot,
            inbound_schedule=state["inbound_schedule"],
            sales_history=state["sales_history"],
            open_orders=state["open_orders"],
            operating_rules=rules,
            inventory_impact=impact,
            at_risk_orders=state["at_risk_orders"],
        ),
        step=step,
    )
    return {
        "option_feasibility": feasibility,
        "option_outcomes": outcomes,
        "visited_nodes": ["response_options"],
    }
