from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.delay_analysis.application.agent.explanation import (
    final_check_input,
    make_explainer,
)
from app.modules.delay_analysis.application.agent.runtime import (
    AnalysisRuntime,
    utc_now_seconds,
)
from app.modules.delay_analysis.application.agent.tools import (
    impact_tools,
    lookup_tools,
)
from app.modules.delay_analysis.application.agent.tools.common import ToolName
from app.modules.delay_analysis.application.agent.tools.context import (
    ToolAttemptTracer,
)
from app.modules.delay_analysis.application.agent.tools.impact_schemas import (
    AtRiskOrders,
    CalculateInventoryImpactInput,
    IdentifyAtRiskOrdersInput,
    InventoryImpact,
)
from app.modules.delay_analysis.application.agent.tools.lookup_schemas import (
    DelayContext,
    GetDelayContextInput,
    InboundSchedule,
    InventorySnapshot,
    OpenOrders,
    OperatingRules,
    SalesHistory,
)
from app.modules.delay_analysis.application.agent.tools.option_comparison import (
    compare_options,
)
from app.modules.delay_analysis.application.agent.tools.option_feasibility import (
    check_option_feasibility,
)
from app.modules.delay_analysis.application.agent.tools.option_outcomes import (
    calculate_option_outcomes,
)
from app.modules.delay_analysis.application.agent.tools.option_schemas import (
    CalculateOptionOutcomesInput,
    CheckOptionFeasibilityInput,
    CompareOptionsInput,
    OptionFeasibility,
    OptionOutcomes,
    OptionRanking,
)
from app.modules.delay_analysis.application.agent.tools.output_validation import (
    validate_agent_output,
)
from app.modules.delay_analysis.application.agent.tools.registry import (
    ToolRegistry,
    ToolSpec,
    pure_tool,
)
from app.modules.delay_analysis.application.agent.tools.validation_schemas import (
    AgentOutputValidation,
    ValidateAgentOutputInput,
)
from app.modules.delay_analysis.application.run_start.transitions import StartedRun
from app.modules.delay_analysis.infrastructure.engine.engine_input import (
    EngineHold,
    EngineInput,
    NoDelay,
    build_engine_input,
    decision_of,
    judge_no_delay,
)

"""
    실제 실행 도구 묶음 조립 (GLOW-128, docs/agent-flow/agent-tool-interface.md 5.1절 "실제 도구 교체")
      - 실행 하나에 묶음 하나. 분석 준비는 실행기의 워커 세션으로 엔진 입력을 만든다(judge_no_delay → build_engine_input).
      - 도구는 develop 에 있는 실제 함수를 등록한다. 조회 T1~T5·T8, 재고 영향 T6·T7, 대응안 T9·T10·T11, 최종 확인 T12.
        계약 도구 12개가 모두 있어 빠진 도구가 없다(missing_engine_tools() == []).
      - T10(GLOW-133, #90)은 묶음 채우기 작업에서 등록했다(사용자 결정 11-10). 실제 묶음으로 끝까지 도는 시험은 평가
        시나리오 정답 대조(GLOW-58 2단계)가 이 함수를 그대로 불러 맡는다. 자동 시작 설정은 그 시험이 통과할 때까지
        기본 꺼짐이다(ANALYSIS_AUTO_START_ENABLED, docs/analysis/auto-start.md 7절).
      - tracer: 실행 추적기(RunTracer). 도구 시도와 설명 시도가 같은 추적기로 간다(ToolContext.tracer). 없으면 추적하지 않는다.
        추적기를 만들고 닫는 것은 부르는 쪽(실행기 tracer_factory)이 한다.
      - 시간 제한은 아직 넣지 않는다(계약 문서에 도구별 값이 없음). 엔진 계산은 엔진 입력을 만들 때 끝나 도구는 옮기기만 한다.
      - clock: 설명 생성 시각. 가상 시각이면 실행 시작 시각(저장 끝난 시각 = 시작 + 1초보다 앞), 아니면 서버 시각.
"""

ENGINE_TOOLS: dict[ToolName, ToolSpec] = {
    ToolName.GET_DELAY_CONTEXT: ToolSpec(
        lookup_tools.get_delay_context, GetDelayContextInput, DelayContext
    ),
    ToolName.GET_INVENTORY_SNAPSHOT: ToolSpec(
        lookup_tools.get_inventory_snapshot, None, InventorySnapshot
    ),
    ToolName.GET_INBOUND_SCHEDULE: ToolSpec(
        lookup_tools.get_inbound_schedule, None, InboundSchedule
    ),
    ToolName.GET_SALES_HISTORY: ToolSpec(
        lookup_tools.get_sales_history, None, SalesHistory
    ),
    ToolName.GET_OPEN_ORDERS: ToolSpec(lookup_tools.get_open_orders, None, OpenOrders),
    ToolName.CALCULATE_INVENTORY_IMPACT: ToolSpec(
        impact_tools.calculate_inventory_impact,
        CalculateInventoryImpactInput,
        InventoryImpact,
    ),
    ToolName.IDENTIFY_AT_RISK_ORDERS: ToolSpec(
        impact_tools.identify_at_risk_orders, IdentifyAtRiskOrdersInput, AtRiskOrders
    ),
    ToolName.GET_OPERATING_RULES: ToolSpec(
        lookup_tools.get_operating_rules, None, OperatingRules
    ),
    ToolName.CHECK_OPTION_FEASIBILITY: ToolSpec(
        pure_tool(check_option_feasibility),
        CheckOptionFeasibilityInput,
        OptionFeasibility,
    ),
    ToolName.CALCULATE_OPTION_OUTCOMES: ToolSpec(
        pure_tool(calculate_option_outcomes),
        CalculateOptionOutcomesInput,
        OptionOutcomes,
    ),
    ToolName.COMPARE_OPTIONS: ToolSpec(
        pure_tool(compare_options), CompareOptionsInput, OptionRanking
    ),
    ToolName.VALIDATE_AGENT_OUTPUT: ToolSpec(
        pure_tool(validate_agent_output),
        ValidateAgentOutputInput,
        AgentOutputValidation,
    ),
}


def missing_engine_tools() -> list[ToolName]:
    """실제 등록표에 없는 계약 도구. 비어 있어야 자동 시작을 켤 수 있다."""
    return ToolRegistry(ENGINE_TOOLS).missing()


@dataclass
class EnginePreparer:
    """분석 준비(AnalysisPreparer): 실행기의 워커 세션으로 엔진 입력을 만든다. 실행 하나에 하나."""

    db: AsyncSession
    run: StartedRun

    async def judge_no_delay(self) -> NoDelay | None:
        return await judge_no_delay(self.db, self.run)

    async def build_engine_input(self) -> EngineInput | EngineHold:
        return await build_engine_input(self.db, self.run)


def build_engine_runtime(
    db: AsyncSession,
    run: StartedRun,
    *,
    clock: Callable[[], datetime] = utc_now_seconds,
    tracer: ToolAttemptTracer | None = None,
) -> AnalysisRuntime:
    """실제 실행 도구 묶음. tracer 는 만들어진 실행 추적기(없으면 추적 안 함)."""
    return AnalysisRuntime(
        registry=ToolRegistry(ENGINE_TOOLS),
        preparer=EnginePreparer(db, run),
        decide=decision_of,
        explain=make_explainer(),
        final_check_input=final_check_input,
        input_seq=run.input_seq,
        tracer=tracer,
        clock=clock,
    )
