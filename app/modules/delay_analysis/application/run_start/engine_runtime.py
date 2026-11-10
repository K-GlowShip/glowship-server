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
from app.modules.delay_analysis.application.agent.tools.option_schemas import (
    CheckOptionFeasibilityInput,
    CompareOptionsInput,
    OptionFeasibility,
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
      - 도구는 develop 에 있는 실제 함수를 등록한다. 조회 T1~T5·T8, 재고 영향 T6·T7, 대응안 T9·T11, 최종 확인 T12.
      - **대응안 결과 T10(GLOW-133)은 아직 등록하지 않는다.** T10 함수는 #90 으로 develop 에 들어왔지만, 실제 묶음으로
        끝까지 도는 시험 전에 등록하면 켜기 확인이 통과해 검증 안 된 경로를 켤 수 있게 된다(사용자 결정 11-10).
        등록표에 T10 이 없으면 AnalysisRuntime 이 만들 때 멈추므로 자동 시작 설정은 기본 꺼짐이고
        (ANALYSIS_AUTO_START_ENABLED), 켜려 하면 서버 시작 때 missing_engine_tools() 로 막는다(사용자 결정 11-09).
        끝까지 시험과 함께 ENGINE_TOOLS 에 한 줄을 더한다(docs/analysis/auto-start.md 7절).
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
    # ToolName.CALCULATE_OPTION_OUTCOMES (T10, option_outcomes.calculate_option_outcomes): 끝까지 시험과 함께 등록한다
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
) -> AnalysisRuntime:
    """실제 실행 도구 묶음. 등록표에 빠진 도구가 있으면 AnalysisRuntime 이 ValueError 로 멈춘다(T10 등록 전에는 항상)."""
    return AnalysisRuntime(
        registry=ToolRegistry(ENGINE_TOOLS),
        preparer=EnginePreparer(db, run),
        decide=decision_of,
        explain=make_explainer(),
        final_check_input=final_check_input,
        input_seq=run.input_seq,
        clock=clock,
    )
