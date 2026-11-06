from langchain_core.runnables import RunnableConfig

from app.modules.delay_analysis.application.agent.runtime import (
    ExplanationFailedLike,
    runtime_of,
)
from app.modules.delay_analysis.application.agent.state import (
    AnalysisOutcome,
    AnalysisState,
    AnalysisStep,
)


async def explain_node(state: AnalysisState, config: RunnableConfig) -> dict:
    """
    추천 근거 설명 노드 (agentflow 5.3·5.4, 인터페이스 문서 2절)
      - 계산 결과를 바꾸거나 새 수치를 만들지 않고, 비교표의 의미와 장단점만 설명합니다(언어 모델, 개발 3 설명 생성).
      - 생성·검증 3회 재시도는 설명 함수 안에서 합니다. 생성 시각은 실행 도구 묶음의 시계가 정합니다.
      - 3회 모두 실패하면 FAILED(EXPLANATION_GENERATION_FAILED·VALIDATING_OUTPUT·3회·소진, #28 explanation_failure)로
        끝내고, 계산 결과는 상태에 남아 서비스가 함께 저장합니다(agentflow 5.5 "설명 검증 계속 실패").
    """
    runtime = runtime_of(config)
    update: dict = {
        "step": AnalysisStep.VALIDATING_OUTPUT,
        "visited_nodes": ["explain"],
    }

    result = await runtime.explain(runtime.ctx, runtime.clock())
    if isinstance(result, ExplanationFailedLike):
        return {
            **update,
            "outcome": AnalysisOutcome.FAILED,
            "failure": result.failure_summary(),
            "explanation_failed": result,
        }
    return {**update, "explanation": result}
