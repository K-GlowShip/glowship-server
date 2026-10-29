from app.modules.delay_analysis.application.agent.state import (
    AnalysisOutcome,
    AnalysisState,
    AnalysisStep,
)


async def coordinator_node(state: AnalysisState) -> dict:
    """
    조정 에이전트
      - 서비스가 고정한 큰 순서(재고 영향 → 대응안 분석 → 비교) 안에서 다음 분석을 정합니다.
      - 하위 에이전트 결과를 받을 때마다 다시 호출되며, 실제 이동은 graph.route_from_coordinator 가 결정합니다.
      - 재고 영향 결과에 위험 주문이 없으면 NO_RISK 로 종료합니다.

    TODO: 지연 문맥 조회(T1), 지연 단계·자료 상태에 따른 분석·도구 선택
    """
    update: dict = {"visited_nodes": ["coordinator"]}

    if "impact_result" not in state:
        update["step"] = AnalysisStep.ANALYZING_IMPACT
    elif not state.get("has_at_risk_orders", False):
        update["outcome"] = AnalysisOutcome.NO_RISK
    elif "option_results" not in state:
        update["step"] = AnalysisStep.ANALYZING_OPTIONS

    return update
