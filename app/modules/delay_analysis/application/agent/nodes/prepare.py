from app.modules.delay_analysis.application.agent.state import (
    AnalysisState,
    AnalysisStep,
)


async def prepare_node(state: AnalysisState) -> dict:
    """
    분석 준비 노드
      - 권한·중복·필수 자료·STALE 여부를 확인하고, 예상 3PL 입고일과 p90 계획 입고일을 다시 비교합니다.
      - 지연이 해소되면 NO_DELAY, 필수 자료가 부족하면 ON_HOLD 로 종료합니다.

    TODO: 검증·재비교 로직 (현재 뼈대는 지연이 유지되고 자료가 충분하다고 가정)
    """
    return {
        "step": AnalysisStep.VALIDATING,
        "visited_nodes": ["prepare"],
    }
