from collections.abc import Mapping
from typing import Any

from langchain_core.runnables import RunnableConfig

from app.modules.delay_analysis.application.agent.runtime import runtime_of
from app.modules.delay_analysis.application.agent.state import (
    AnalysisState,
    AnalysisStep,
)
from app.modules.delay_analysis.application.agent.tools.common import ToolName
from app.modules.delay_analysis.application.agent.tools.option_schemas import (
    CompareOptionsInput,
    OptionComparisonInput,
    OptionOutcomes,
    OptionRanking,
)
from app.modules.delay_analysis.application.agent.tools.registry import code_error

# 엔진 판정 서명의 대응안 항목(엔진 pipeline.signature, 공유 묶음 glowship_engine/pipeline.py 49~52행)과 같은 이름의 T10 필드
SIGNED_OPTION_FIELDS = (
    "feasibility_status",
    "reason_code",
    "delay_induced_recovered_orders",
    "reduction_committed_total",
)


def option_differences(
    outcomes: OptionOutcomes, ranking: OptionRanking, analysis: Mapping[str, Any]
) -> list[str]:
    """
    T10·T11 출력이 엔진 판정 서명(analysis["signature"])과 다른 곳. 빈 목록이면 같다.
    판정 서명은 엔진 결과로 만들어지므로(엔진 입력 decision_of), 대응안 도구(A 의 순수 함수) 결과가 다르면
    화면에 보일 값과 STALE 판정 서명이 어긋난다(인터페이스 문서 2절, 최종 분담안 v2).
    """
    signature = analysis["signature"]
    engine = {o["option_type"]: o for o in signature["options"]}
    tool = {str(o.option_type): o for o in outcomes.options}
    diffs: list[str] = []
    if set(engine) != set(tool):
        diffs.append(f"대응안 목록: 엔진 {sorted(engine)} · T10 {sorted(tool)}")
    for option_type in sorted(set(engine) & set(tool)):
        for key in SIGNED_OPTION_FIELDS:
            expected, actual = engine[option_type][key], getattr(tool[option_type], key)
            if expected != actual:
                diffs.append(f"{option_type}.{key}: 엔진 {expected} · T10 {actual}")
    recommended = next(
        (str(o.option_type) for o in ranking.options if o.is_recommended), None
    )
    if recommended != signature["recommended_option_type"]:
        diffs.append(
            f"추천: 엔진 {signature['recommended_option_type']} · T11 {recommended}"
        )
    return diffs


async def compare_node(state: AnalysisState, config: RunnableConfig) -> dict:
    """
    대응안 비교 노드 (agentflow 5.3, 인터페이스 문서 2절)
      - T10 대응안 결과에서 순위에 쓰는 값만 골라(계약 7절) T11 로 추천 순위를 매깁니다.
      - T10·T11 출력을 엔진 판정 서명과 대조해 다르면 FAILED(UNEXPECTED_TOOL_ERROR)로 멈춥니다.
      - 같으면 판정 서명·의존 범위를 만듭니다(엔진 입력 decision_of, 결정 2). 설명이 판정 서명 SHA-256 을 쓰므로 설명보다 먼저입니다.
      - 실패 단계는 VALIDATING_OUTPUT(비교·설명·결과 검증, 인터페이스 5절).
    """
    runtime = runtime_of(config)
    step = AnalysisStep.VALIDATING_OUTPUT
    outcomes = state["option_outcomes"]

    ranking = await runtime.call(
        ToolName.COMPARE_OPTIONS,
        CompareOptionsInput(
            options=[
                OptionComparisonInput(
                    option_type=o.option_type,
                    feasibility_status=o.feasibility_status,
                    delay_induced_recovered_orders=o.delay_induced_recovered_orders,
                    reduction_committed_total=o.reduction_committed_total,
                    expected_delivery_date=o.expected_delivery_date,
                )
                for o in outcomes.options
            ]
        ),
        step=step,
    )
    diffs = option_differences(outcomes, ranking, runtime.ctx.engine.analysis)
    if diffs:
        raise code_error(
            ToolName.COMPARE_OPTIONS.value,
            step,
            1,
            "대응안 결과가 엔진 판정 서명과 다름: " + "; ".join(diffs),
        )
    return {
        "option_ranking": ranking,
        "decision": runtime.decide(runtime.ctx.engine),
        "step": step,
        "visited_nodes": ["compare"],
    }
