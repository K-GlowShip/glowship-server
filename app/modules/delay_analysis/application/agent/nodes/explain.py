from app.modules.delay_analysis.application.agent.state import AnalysisState


async def explain_node(state: AnalysisState) -> dict:
    """
    추천 근거 설명 노드
      - 계산 결과를 바꾸거나 새 수치를 만들지 않고, 비교표의 의미와 장단점만 설명합니다(언어 모델).

    TODO: 언어 모델 호출과 출력 JSON 스키마 (현재 뼈대는 빈 설명 반환)
    """
    return {
        "explanation": {},
        "visited_nodes": ["explain"],
    }
