import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from pydantic import BaseModel

from app.modules.delay_analysis.application.agent.retry import (
    FailureSummary,
    explanation_failure,
)
from app.modules.delay_analysis.application.agent.runtime import AnalysisRuntime
from app.modules.delay_analysis.application.agent.tools.common import ToolName
from app.modules.delay_analysis.application.agent.tools.context import (
    ToolAttemptTracer,
    ToolContext,
)
from app.modules.delay_analysis.application.agent.tools.impact_schemas import (
    AtRiskOrder,
    AtRiskOrderLine,
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
    LeadTimes,
    OpenOrders,
    OperatingRules,
    SalesHistory,
)
from app.modules.delay_analysis.application.agent.tools.option_schemas import (
    CalculateOptionOutcomesInput,
    CheckOptionFeasibilityInput,
    CompareOptionsInput,
    OptionFeasibility,
    OptionOutcome,
    OptionOutcomes,
    OptionPrecheck,
    OptionRank,
    OptionRanking,
    Precheck,
)
from app.modules.delay_analysis.application.agent.tools.registry import ToolSpec
from app.modules.delay_analysis.infrastructure.analysis_enums import (
    CompletedStatus,
    FeasibilityStatus,
    OptionType,
    ResultClass,
    RiskAttribution,
)
from app.modules.shipment.infrastructure.shipment_enums import (
    ShipmentStage,
    TransportMode,
)

"""
    개발·시험용 가짜 실행 도구 묶음 (GLOW-129). dry-run API(GLOW-46)와 그래프 시험이 쓴다. 실제 실행에는 쓰지 않는다.
      - 도구 12개·분석 준비·판정 서명·설명을 가짜로 채워 그래프가 끝까지 흐르게 한다.
      - 가짜 도구 출력은 model_construct 로 만든다(검증 없이, 노드가 읽는 필드만 채움). 이 묶음은 흐름
        (부르는 순서·종료 갈래·실패 처리)을 보는 용도다. 출력 값이 계약에 맞는지는 도구를 만드는 쪽 시험
        (개발 1 정답 대조, A 엔진 대조, #26 모델 시험)과 실제 도구로 바꾼 뒤 통합 시험(T14)이 본다.
      - 노드가 만드는 도구 입력(T7·T6·T9·T10·T11)은 실제 계약 모델이라 검증된다. Pydantic 은 이미 만든 모델을
        다른 모델에 넣을 때 필드 검사는 다시 하지 않지만 모델 뒤 검사(model_validator after)는 다시 돌린다.
        그래서 다음 입력에 들어가는 가짜(T7·T9·T6)는 그 뒤 검사를 통과하게 채운다.
"""

BASE_DATE = date(2026, 1, 2)  # 가짜 기준일 D
_NIL_ID = uuid.UUID(int=0)  # 가짜 실행·지연 이벤트 ID(도구 가짜는 읽지 않음)
ENGINE_OPTION_FIELDS = (
    "feasibility_status",
    "reason_code",
    "delay_induced_recovered_orders",
    "reduction_committed_total",
)


@dataclass(frozen=True)
class FakeScenario:
    """가짜 묶음이 흉내 낼 실행. 기본값은 READY_FOR_REVIEW(위험 주문 있음, 다른 권역 출고 추천)."""

    no_delay: bool = False
    hold_code: str | None = None  # 보류 코드(retry.HOLD_CODES)면 ON_HOLD
    completed_status: CompletedStatus = CompletedStatus.READY_FOR_REVIEW
    result_class: ResultClass = ResultClass.READY_ORDER_RISK
    has_at_risk_orders: bool = True
    explanation_fails: bool = False
    final_check_passes: bool = True
    engine_recommended: OptionType | None = (
        OptionType.OTHER_REGION_FULFILL
    )  # 엔진 판정 서명의 추천


@dataclass(frozen=True)
class FakeEngineInput:
    """EngineInputLike 와 같은 칸(DV1-5). 실행 ID 들은 가짜 값이고 도구 가짜는 읽지 않는다."""

    snap: dict[str, Any]
    analysis: dict[str, Any]
    analysis_run_id: uuid.UUID = _NIL_ID
    delay_event_id: uuid.UUID = _NIL_ID
    base_date: date = BASE_DATE
    input_seq: int = 1
    store: Any = None


@dataclass(frozen=True)
class FakeHold:
    code: str
    message: str


@dataclass(frozen=True)
class FakeNoDelay:
    decision_signature_sha256: str = "0" * 64


@dataclass(frozen=True)
class FakeDecision:
    decision_signature: dict[str, Any]
    dependency_scope: dict[str, Any] | None


@dataclass(frozen=True)
class FakeExplanation:
    model_name: str
    prompt_version: str
    final_explanation_json: dict[str, Any]
    generated_at: datetime


@dataclass(frozen=True)
class FakeExplanationFailed:
    model_name: str
    prompt_version: str
    failure_message: str

    def failure_summary(self) -> FailureSummary:
        return explanation_failure(self.failure_message)


class FakeFinalCheckInput(BaseModel):
    explanation: dict[str, Any]


class FakeValidationError(BaseModel):
    code: str
    location: str


class FakeValidation(BaseModel):
    status: str
    errors: list[FakeValidationError]


def _options() -> list[OptionOutcome]:
    keep = OptionOutcome.model_construct(
        option_type=OptionType.KEEP_PLAN,
        feasibility_status=FeasibilityStatus.FEASIBLE,
        reason_code=None,
        delay_induced_recovered_orders=0,
        reduction_committed_total=0,
        expected_delivery_date=None,
    )
    other = OptionOutcome.model_construct(
        option_type=OptionType.OTHER_REGION_FULFILL,
        feasibility_status=FeasibilityStatus.FEASIBLE,
        reason_code=None,
        delay_induced_recovered_orders=2,
        reduction_committed_total=5,
        expected_delivery_date=BASE_DATE + timedelta(days=3),
    )
    return [keep, other]


def _engine(scenario: FakeScenario) -> FakeEngineInput:
    ready = scenario.completed_status == CompletedStatus.READY_FOR_REVIEW
    options = (
        [
            {"option_type": str(o.option_type)}
            | {key: getattr(o, key) for key in ENGINE_OPTION_FIELDS}
            for o in _options()
        ]
        if ready
        else []
    )
    signature = {
        "schema_version": "decision_signature.v1",
        "status": str(scenario.completed_status),
        "result_class": str(scenario.result_class),
        "options": options,
        "recommended_option_type": str(scenario.engine_recommended)
        if ready and scenario.engine_recommended
        else None,
    }
    return FakeEngineInput(
        snap={},
        analysis={
            "status": str(scenario.completed_status),
            "signature": signature,
            "sha": "0" * 64,
            "scope": {"schema_version": "dependency_scope.v1"},
            "options": options,
        },
    )


@dataclass
class FakePreparer:
    scenario: FakeScenario
    engine: FakeEngineInput
    calls: list[str] = field(default_factory=list)

    async def judge_no_delay(self) -> FakeNoDelay | None:
        self.calls.append("judge_no_delay")
        return FakeNoDelay() if self.scenario.no_delay else None

    async def build_engine_input(self) -> FakeEngineInput | FakeHold:
        self.calls.append("build_engine_input")
        if self.scenario.hold_code:
            return FakeHold(self.scenario.hold_code, "가짜 보류: 필수 자료 부족")
        return self.engine


def fake_tools(scenario: FakeScenario) -> dict[ToolName, ToolSpec]:
    """도구 12개의 가짜. 시험은 필요한 도구만 바꿔 끼운다(fake_runtime(tools=...))."""

    async def t1(ctx: ToolContext, payload: GetDelayContextInput) -> DelayContext:
        return DelayContext.model_construct(
            shipment_id=str(uuid.UUID(int=1)),
            base_date=payload.base_date,
            planned_3pl_inbound_date=BASE_DATE,
            expected_3pl_inbound_date=BASE_DATE + timedelta(days=5),
            stage=ShipmentStage.AT_SEA,
            transport_mode=TransportMode.OCEAN,
            planned_kr_release_date=BASE_DATE - timedelta(days=20),
            shipment_skus=[],
        )

    async def t2(ctx: ToolContext, payload: None) -> InventorySnapshot:
        return InventorySnapshot.model_construct(
            inventory_date=BASE_DATE - timedelta(days=1), warehouses=[], kr_on_hand=[]
        )

    async def t3(ctx: ToolContext, payload: None) -> InboundSchedule:
        return InboundSchedule.model_construct(inbound=[], donor_next_inbound=[])

    async def t4(ctx: ToolContext, payload: None) -> SalesHistory:
        return SalesHistory.model_construct()

    async def t5(ctx: ToolContext, payload: None) -> OpenOrders:
        return OpenOrders.model_construct()

    async def t8(ctx: ToolContext, payload: None) -> OperatingRules:
        return OperatingRules.model_construct(
            lead_times=LeadTimes.model_construct(), skus=[]
        )

    async def t7(ctx: ToolContext, payload: IdentifyAtRiskOrdersInput) -> AtRiskOrders:
        risks = (
            [
                AtRiskOrder.model_construct(
                    risk_attribution=RiskAttribution.DELAY_INDUCED,
                    lines=[AtRiskOrderLine.model_construct(sku_code="SKU-1")],
                )
            ]
            if scenario.has_at_risk_orders
            else []
        )
        return AtRiskOrders.model_construct(
            risks=risks,
            delay_induced_sku_codes=["SKU-1"] if scenario.has_at_risk_orders else [],
        )

    async def t6(
        ctx: ToolContext, payload: CalculateInventoryImpactInput
    ) -> InventoryImpact:
        return InventoryImpact.model_construct(
            items=[],
            impacted_sku_codes=["SKU-1"],
            watch_sku_codes=[],
            completed_status=scenario.completed_status,
            result_class=scenario.result_class,
        )

    async def t9(
        ctx: ToolContext, payload: CheckOptionFeasibilityInput
    ) -> OptionFeasibility:
        return OptionFeasibility.model_construct(
            options=[
                OptionPrecheck.model_construct(
                    option_type=option_type, precheck=Precheck.CALCULATE
                )
                for option_type in (
                    OptionType.KEEP_PLAN,
                    OptionType.OTHER_REGION_FULFILL,
                )
            ]
        )

    async def t10(
        ctx: ToolContext, payload: CalculateOptionOutcomesInput
    ) -> OptionOutcomes:
        return OptionOutcomes.model_construct(options=_options())

    async def t11(ctx: ToolContext, payload: CompareOptionsInput) -> OptionRanking:
        return OptionRanking.model_construct(
            options=[
                OptionRank.model_construct(
                    option_type=OptionType.KEEP_PLAN,
                    recommendation_rank=2,
                    is_recommended=False,
                ),
                OptionRank.model_construct(
                    option_type=OptionType.OTHER_REGION_FULFILL,
                    recommendation_rank=1,
                    is_recommended=True,
                ),
            ]
        )

    async def t12(ctx: ToolContext, payload: FakeFinalCheckInput) -> FakeValidation:
        if scenario.final_check_passes:
            return FakeValidation(status="PASSED", errors=[])
        return FakeValidation(
            status="FAILED",
            errors=[
                FakeValidationError(code="NUMBER_MISMATCH", location="risk_summary#s1")
            ],
        )

    return {
        ToolName.GET_DELAY_CONTEXT: ToolSpec(t1, GetDelayContextInput, DelayContext),
        ToolName.GET_INVENTORY_SNAPSHOT: ToolSpec(t2, None, InventorySnapshot),
        ToolName.GET_INBOUND_SCHEDULE: ToolSpec(t3, None, InboundSchedule),
        ToolName.GET_SALES_HISTORY: ToolSpec(t4, None, SalesHistory),
        ToolName.GET_OPEN_ORDERS: ToolSpec(t5, None, OpenOrders),
        ToolName.CALCULATE_INVENTORY_IMPACT: ToolSpec(
            t6, CalculateInventoryImpactInput, InventoryImpact
        ),
        ToolName.IDENTIFY_AT_RISK_ORDERS: ToolSpec(
            t7, IdentifyAtRiskOrdersInput, AtRiskOrders
        ),
        ToolName.GET_OPERATING_RULES: ToolSpec(t8, None, OperatingRules),
        ToolName.CHECK_OPTION_FEASIBILITY: ToolSpec(
            t9, CheckOptionFeasibilityInput, OptionFeasibility
        ),
        ToolName.CALCULATE_OPTION_OUTCOMES: ToolSpec(
            t10, CalculateOptionOutcomesInput, OptionOutcomes
        ),
        ToolName.COMPARE_OPTIONS: ToolSpec(t11, CompareOptionsInput, OptionRanking),
        ToolName.VALIDATE_AGENT_OUTPUT: ToolSpec(
            t12, FakeFinalCheckInput, FakeValidation
        ),
    }


async def _no_sleep(_: float) -> None:
    return None


def fake_runtime(
    scenario: FakeScenario | None = None,
    *,
    tools: Mapping[ToolName, ToolSpec] | None = None,
    tracer: ToolAttemptTracer | None = None,
) -> AnalysisRuntime:
    """가짜 실행 도구 묶음. 실행마다 새로 만든다(묶음 하나 = 실행 하나). tools 로 일부 도구만 바꿔 끼운다."""
    scenario = scenario or FakeScenario()
    engine = _engine(scenario)

    async def explain(ctx: ToolContext, now: datetime) -> Any:
        if scenario.explanation_fails:
            return FakeExplanationFailed(
                "fake/model", "fake@1", "1:FAKE@s1;2:FAKE@s1;3:FAKE@s1"
            )
        return FakeExplanation(
            "fake/model",
            "fake@1",
            {"decision_signature_hash": ctx.engine.analysis["sha"]},
            now,
        )

    return AnalysisRuntime(
        registry={**fake_tools(scenario), **(tools or {})},
        preparer=FakePreparer(scenario, engine),
        decide=lambda e: FakeDecision(e.analysis["signature"], e.analysis["scope"]),
        explain=explain,
        final_check_input=lambda passed: FakeFinalCheckInput(
            explanation=passed.final_explanation_json
        ),
        base_date=BASE_DATE,
        input_seq=1,
        tracer=tracer,
        clock=lambda: datetime(2026, 1, 2, 9, 0, tzinfo=UTC),
        sleep=_no_sleep,
    )
