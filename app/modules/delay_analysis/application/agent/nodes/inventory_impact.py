from app.modules.delay_analysis.application.agent.state import AnalysisState


async def inventory_impact_node(state: AnalysisState) -> dict:
    """
    재고 영향 분석 에이전트
      - 재고·입고 일정·최근 28일 판매량·미출고 주문·운영 기준을 조회하고(T2·T3·T4·T5·T8)
        안전재고 하회일·재고 소진일·품절 기간·부족 수량과 위험 주문을 계산합니다(T6·T7).
      - 결과는 조정 에이전트로 돌아갑니다.

    TODO: 조회·계산 도구 연결 (현재 뼈대는 빈 결과와 위험 주문 있음으로 반환)
    """
    return {
        "impact_result": {},
        "has_at_risk_orders": True,
        "visited_nodes": ["inventory_impact"],
    }
