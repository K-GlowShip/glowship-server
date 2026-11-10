from langchain_core.runnables import RunnableConfig

from app.modules.delay_analysis.application.agent.runtime import runtime_of
from app.modules.delay_analysis.application.agent.state import (
    AnalysisOutcome,
    AnalysisState,
    AnalysisStep,
)
from app.modules.delay_analysis.application.agent.tools.common import ToolName
from app.modules.delay_analysis.application.agent.tools.registry import code_error

PASSED = "PASSED"  # T12 판정 통과(T12 AgentOutputStatus.PASSED)


async def validate_node(state: AnalysisState, config: RunnableConfig) -> dict:
    """
    결과 검증 노드 (agentflow 3절·5.3, 인터페이스 문서 2절)
      - 통과한 설명을 T12 validate_agent_output 로 한 번 더 확인합니다(최종 설명 확인). 통과하면 READY_FOR_REVIEW.
      - 설명 함수가 이미 같은 검증으로 통과시킨 설명이 여기서 떨어지면 두 검증이 어긋난 연결 오류라
        FAILED(UNEXPECTED_TOOL_ERROR)로 멈춥니다. "3회 실패"(EXPLANATION_GENERATION_FAILED)는 실제 시도가 3번이 아니라 쓰지 않습니다.
      - 실패 단계는 VALIDATING_OUTPUT.
    """
    runtime = runtime_of(config)
    step = AnalysisStep.VALIDATING_OUTPUT
    explanation = state["explanation"]

    verdict = await runtime.call(
        ToolName.VALIDATE_AGENT_OUTPUT,
        runtime.final_check_input(explanation),
        step=step,
    )
    if str(verdict.status) != PASSED:
        errors = ", ".join(f"{e.code}@{e.location}" for e in verdict.errors)
        raise code_error(
            ToolName.VALIDATE_AGENT_OUTPUT.value,
            step,
            1,
            f"설명 생성 때 통과한 설명이 최종 확인에서 {verdict.status}: {errors}",
        )
    return {
        "step": step,
        "outcome": AnalysisOutcome.READY_FOR_REVIEW,
        "visited_nodes": ["validate"],
    }
