from app.modules.delay_analysis.application.agent.state import AnalysisState


async def compare_node(state: AnalysisState) -> dict:
    """
    대응안 비교 노드
      - 기존 계획과 대응안별 결과를 같은 주문 범위·같은 기준으로 비교표로 만듭니다(T11).

    TODO: 비교 도구 연결 (현재 뼈대는 빈 비교표 반환)
    """
    return {
        "comparison": {},
        "visited_nodes": ["compare"],
    }
