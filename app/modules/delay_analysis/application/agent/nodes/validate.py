from app.modules.delay_analysis.application.agent.state import (
    AnalysisOutcome,
    AnalysisState,
    AnalysisStep,
)


async def validate_node(state: AnalysisState) -> dict:
    """
    결과 검증 노드
      - 도구 결과·비교표·AI 설명의 필수 필드·상태·수치 일치 여부를 검사합니다(T12).
      - 통과하면 READY_FOR_REVIEW, 설명만 실패하면 계산 결과와 설명 실패 상태를 반환합니다.

    TODO: 검증 도구 연결과 설명 재요청 (현재 뼈대는 항상 통과)
    """
    return {
        "step": AnalysisStep.VALIDATING_OUTPUT,
        "outcome": AnalysisOutcome.READY_FOR_REVIEW,
        "visited_nodes": ["validate"],
    }
