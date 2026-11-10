from langchain_core.runnables import RunnableConfig

from app.modules.delay_analysis.application.agent.runtime import runtime_of
from app.modules.delay_analysis.application.agent.state import (
    AnalysisOutcome,
    AnalysisState,
    AnalysisStep,
)
from app.modules.delay_analysis.application.agent.tools.common import ToolName
from app.modules.delay_analysis.application.agent.tools.lookup_schemas import (
    GetDelayContextInput,
)
from app.modules.delay_analysis.infrastructure.analysis_enums import CompletedStatus


async def coordinator_node(state: AnalysisState, config: RunnableConfig) -> dict:
    """
    조정 에이전트 (agentflow 3절·5.2)
      - 서비스가 고정한 큰 순서(재고 영향 → 대응안 분석 → 비교) 안에서 다음 분석을 정합니다.
      - 하위 에이전트 결과를 받을 때마다 다시 호출되며, 실제 이동은 graph.route_from_coordinator 가 결정합니다.
      - 처음 들어오면 T1 지연 문맥을 조회합니다(agentflow 5.2 "O→Q [T1]").
      - 재고 영향 결과(T6)의 완료 판정이 NO_RISK 면 판정 서명·의존 범위를 만들고 NO_RISK 로 끝냅니다
        (agentflow 5.5 출력 표 "위험 주문 없음 = 재고 영향 결과·결과 분류·의존 범위·판정 서명").
        위험 주문이 없어도 예측상 부족만 있으면(FORECAST_ONLY_READY) READY 라 대응안 분석으로 갑니다.
    """
    runtime = runtime_of(config)
    update: dict = {"visited_nodes": ["coordinator"]}

    if "inventory_impact" not in state:
        update["step"] = AnalysisStep.ANALYZING_IMPACT
        if "delay_context" not in state:
            update["delay_context"] = await runtime.call(
                ToolName.GET_DELAY_CONTEXT,
                GetDelayContextInput(
                    analysis_run_id=str(state["analysis_run_id"]),
                    delay_event_id=str(state["delay_event_id"]),
                    base_date=runtime.ctx.base_date,
                    input_seq=runtime.input_seq,
                ),
                step=AnalysisStep.ANALYZING_IMPACT,
            )
    elif state["inventory_impact"].completed_status == CompletedStatus.NO_RISK:
        update["decision"] = runtime.decide(runtime.ctx.engine)
        update["outcome"] = AnalysisOutcome.NO_RISK
    elif "option_outcomes" not in state:
        update["step"] = AnalysisStep.ANALYZING_OPTIONS

    return update
