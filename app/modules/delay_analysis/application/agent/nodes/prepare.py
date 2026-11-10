from langchain_core.runnables import RunnableConfig

from app.modules.delay_analysis.application.agent.retry import hold_summary
from app.modules.delay_analysis.application.agent.runtime import (
    EngineHoldLike,
    runtime_of,
)
from app.modules.delay_analysis.application.agent.state import (
    AnalysisOutcome,
    AnalysisState,
    AnalysisStep,
)


async def prepare_node(state: AnalysisState, config: RunnableConfig) -> dict:
    """
    분석 준비 노드 (agentflow 3절·5.2·5.4, docs/agent-flow/agent-tool-interface.md 2절)
      1) 지연 해소 판정(엔진 입력 judge_no_delay): 해소면 NO_DELAY 로 끝냄. 엔진 저장소를 읽기 전에 한다(엔진 하네스 순서).
      2) 엔진 입력 만들기(build_engine_input): 보류면 ON_HOLD(VALIDATING)로 끝냄.
         보류는 이 단계에서만 낸다(결정 1). 실패 요약은 #28 hold_summary(보류 코드·판정한 쪽 문구).
      3) 엔진 입력을 실행 도구 묶음에 한 번 담는다(뒤 노드의 도구 문맥 ctx.engine).
    권한·중복은 실행 생성(#21, 고객사 잠금 L1)이, STALE 은 조정 에이전트 진입 확인이 맡는다.
    """
    runtime = runtime_of(config)
    update: dict = {"step": AnalysisStep.VALIDATING, "visited_nodes": ["prepare"]}

    no_delay = await runtime.preparer.judge_no_delay()
    if no_delay is not None:
        return {**update, "outcome": AnalysisOutcome.NO_DELAY, "no_delay": no_delay}

    built = await runtime.preparer.build_engine_input()
    if isinstance(built, EngineHoldLike):
        return {
            **update,
            "outcome": AnalysisOutcome.ON_HOLD,
            "failure": hold_summary(built.code, built.message),
        }

    runtime.start(
        company_id=state["company_id"],
        analysis_run_id=state["analysis_run_id"],
        engine=built,
    )
    return update
