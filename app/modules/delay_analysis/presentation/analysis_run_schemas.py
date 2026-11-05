import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import (
    Field,
    PlainSerializer,
    StrictInt,
    StrictStr,
)

from app.core.schemas import CamelModel
from app.core.summary_schemas import ShipmentSummary, SkuSummary, WarehouseSummary
from app.modules.delay_analysis.infrastructure.analysis_enums import (
    AnalysisRunStatus,
    CalculationStatus,
    CompletedStatus,
    ExplanationValidationStatus,
    ExplanationWarningCode,
    FailedStep,
    FeasibilityStatus,
    LineSecurementStatus,
    NotRecoveredReason,
    OptionReasonCode,
    OptionType,
    OrderOutcome,
    ResultClass,
    RiskAttribution,
    SalesBasisType,
    SkuScope,
    StaleReason,
    StaleReasonCode,
    SupplySource,
    TriggerReason,
    TriggerType,
    WaitCause,
)
from app.modules.delay_analysis.infrastructure.stored_json_schemas import (  # noqa: F401  (SkuResults 등은 여기서도 내보냄)
    RawDecimal,
    SkuResultDonor,
    SkuResultItem,
    SkuResults,
    StoredJsonModel,
    StoredUuid,
)

"""
    GET /api/v1/analysis-runs/{runId} 응답 스키마 (명세 v6.2 4.5절)
      - 응답 필드는 camelCase(D2). 저장 위치는 ERD v6.2 analysis_runs 와 분석 결과 4개 테이블.
      - 저장된 JSON(explanation.v1, sku_results.v1)은 저장된 그대로 snake_case 키로 내려준다(D20).
        공용 타입(StoredJsonModel·RawDecimal·StoredUuid)과 sku_results.v1 모델은 infrastructure/stored_json_schemas.py 에 있다
        (에이전트 도구 모델과 함께 쓰기 위해 GLOW-112 에서 옮김).
        데이터 팀 계약이 키를 고정하고 알 수 없는 키를 금지하므로, 이 두 모델은 CamelModel 을 쓰지 않고 extra="forbid" 로 둔다.
        저장 JSON 의 정수는 StrictInt 로 받아 불리언·문자열을 정수로 바꿔 쓰지 않는다.
      - 응답에 넣지 않는 DB 열: dependency_scope, decision_signature, decision_signature_sha256, raw_*_shortage_quantity (명세 4.5)
        explanation.v1 안의 decision_signature_hash·prompt_template_sha256 은 계약의 필수 메타데이터라 그대로 둔다
        (명세 4.5 AI 설명 표: "표시하지 않음" = 화면에 표시하지 않음).
"""

# to_camel 은 "3pl" 다음 글자를 대문자로 만든다(planned3PlInboundDate). 명세 표기(planned3plInboundDate)로 별칭을 고정한다.
PLANNED_3PL = "planned3plInboundDate"
EXPECTED_3PL = "expected3plInboundDate"
BASELINE_PLANNED_3PL = "baselinePlanned3plInboundDate"

# DB numeric 값. 서버 안에서는 Decimal 로 정확히 두고, JSON 응답에서만 숫자(number)로 내보낸다.
# Pydantic 기본값은 Decimal 을 문자열("1.2500")로 내보내 프론트가 정렬·비교에 쓰기 불편하다.
JsonNumberDecimal = Annotated[
    Decimal, PlainSerializer(float, return_type=float, when_used="json")
]


# --- explanation.v1 (analysis_runs.final_explanation_json) -------------------------------------


class ExplanationOption(StoredJsonModel):
    option_type: OptionType
    text: str = Field(min_length=1, max_length=600)


class ExplanationWarning(StoredJsonModel):
    code: ExplanationWarningCode
    text: str = Field(min_length=1, max_length=600)


class ExplanationEvidence(StoredJsonModel):
    """문장 속 근거 표지 [E1]… 이 가리키는 설명 입력 문서의 경로(JSON Pointer)와 값"""

    id: str = Field(pattern=r"^E[1-9][0-9]?$")
    path: str = Field(pattern=r"^(/[A-Za-z0-9_\-]+)+$")
    # 스키마 타입은 string·integer·null. bool 은 int 의 하위 타입이라 Strict 로 막는다(True → 1 방지)
    value: StrictStr | StrictInt | None


class Explanation(StoredJsonModel):
    """AI 설명 (explanation.v1). 설명 검증을 통과한 실행에만 있다. 지연 원인 필드는 없다(명세 4.5, AIR-006)."""

    schema_version: Literal["explanation.v1"]
    language: Literal["ko"]
    analysis_run_id: StoredUuid
    decision_signature_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_provider: str = Field(min_length=1, max_length=64)
    model_id: str = Field(min_length=1, max_length=128)
    prompt_template_id: str = Field(pattern=r"^[a-z0-9_]+$")
    prompt_template_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    prompt_template_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    generated_at: str = Field(
        pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
    )
    retry_no: StrictInt = Field(ge=1, le=3)
    run_summary: str = Field(min_length=1, max_length=600)
    risk_summary: str = Field(min_length=1, max_length=600)
    option_explanations: list[ExplanationOption] = Field(min_length=1, max_length=4)
    recommendation_explanation: str = Field(min_length=1, max_length=600)
    warnings: list[ExplanationWarning] = Field(max_length=5)
    evidence_references: list[ExplanationEvidence] = Field(min_length=1, max_length=40)


# --- 응답 묶음 ---------------------------------------------------------------------------------


class AnalysisTarget(CamelModel):
    """
    분석 대상 지연 (명세 4.5 대상 칸). 값은 지연 목록·상세(4.4)와 같은 계산 함수(D13, delay_display)로 만든다.
      - 선적·창고·대표 상품은 지연 목록과 같은 요약 객체(app/core/summary_schemas)다.
      - 단계·입고일·지연 일수는 선적의 현재 값이다(지연 목록과 같음).
    """

    delay_event_id: uuid.UUID
    episode_no: int
    shipment: ShipmentSummary
    warehouse: WarehouseSummary = Field(description="도착 창고")
    representative_sku: SkuSummary | None = Field(
        description="선적에서 수량이 가장 많은 SKU (같으면 상세 줄 번호가 작은 SKU, 상세가 없으면 null)"
    )
    route: str = Field(description="예: KR → LOS ANGELES (D13)")
    stage: str = Field(
        description="선적의 현재 운송 단계 (PRE_DEPARTURE·AT_SEA·IN_AIR·AT_PORT·RECEIVED)"
    )
    planned_3pl_inbound_date: date = Field(alias=PLANNED_3PL)
    expected_3pl_inbound_date: date = Field(
        alias=EXPECTED_3PL, description="선적의 현재 예상 3PL 입고일"
    )
    delay_days: int = Field(
        description="예상 3PL 입고일 − 계획 3PL 입고일 (일). 회복되어 예상일 ≤ 계획일이면 0"
    )


class DataVersion(CamelModel):
    """입력 확정 때 사용한 수집 (analysis_run_data_versions → data_sync_runs)"""

    data_sync_run_id: uuid.UUID
    source_domain: str
    finished_at: datetime | None


class DataWarning(CamelModel):
    """입력 확정 때 마지막 수집이 실패·지연이던 원천"""

    source_domain: str
    last_status: str
    last_finished_at: datetime | None


class InputBasis(CamelModel):
    input_seq: int
    base_date: date | None = Field(
        description="기준일 D. dependency_scope.base_date 에서 읽는다. "
        "ON_HOLD·FAILED 와 진행 중에 STALE 이 된 실행은 dependency_scope 가 없어 null."
    )
    operating_rule_set_id: uuid.UUID
    operating_rule_version_no: int
    calculation_contract_version: str = Field(examples=["T14-v1"])
    allocation_rule_version: str = Field(examples=["D00-B-b2-v1"])
    data_versions: list[DataVersion]
    data_basis_at: datetime | None = Field(
        description="사용한 수집 중 가장 오래된 수집 시각"
    )
    data_warnings: list[DataWarning]


class InventoryImpact(CamelModel):
    """SKU·창고별 재고 영향. 기준선(baseline_*) = 계획 입고일에 들어왔을 때, 전체 = 예상 입고일 반영."""

    sku_id: uuid.UUID
    sku_code: str
    product_name: str
    warehouse_id: uuid.UUID
    warehouse_code: str
    sku_scope: SkuScope
    calculation_status: CalculationStatus
    forecast_method: str
    available_quantity: int
    committed_open_quantity: int | None
    sales_basis_type: SalesBasisType
    sales_basis_days: int
    sales_basis_start_date: date | None
    sales_basis_end_date: date | None
    sales_basis_event_days: int | None
    sales_basis_event_names: list[str] | None
    average_daily_sales: JsonNumberDecimal | None
    safety_stock_days: int
    safety_stock_quantity: int | None
    baseline_planned_3pl_inbound_date: date = Field(alias=BASELINE_PLANNED_3PL)
    expected_3pl_inbound_date: date = Field(alias=EXPECTED_3PL)
    baseline_safety_stock_breach_date: date | None
    baseline_stockout_date: date | None
    baseline_stockout_days: int | None
    baseline_shortage_quantity: int | None
    safety_stock_breach_date: date | None
    stockout_date: date | None
    stockout_days: int | None
    shortage_quantity: int | None


class AtRiskOrderLine(CamelModel):
    at_risk_order_id: uuid.UUID
    order_line_id: uuid.UUID
    line_no: int
    sku_id: uuid.UUID
    sku_code: str
    open_quantity: int
    wait_cause: WaitCause | None


class AtRiskOrder(CamelModel):
    """위험 주문. at_risk_orders 는 주문 상세 1행이므로 주문으로 묶어 내려준다(D21)."""

    order_id: uuid.UUID
    order_code: str
    risk_attribution: RiskAttribution
    promised_delivery_at: datetime
    baseline_expected_delivery_at: datetime | None
    expected_delivery_at: datetime
    expected_beyond_horizon: bool
    risk_reason: str
    lines: list[AtRiskOrderLine]


class OptionOrderOutcome(CamelModel):
    """대응안별 주문 상세 결과 (option_order_outcomes)"""

    at_risk_order_id: uuid.UUID
    outcome: OrderOutcome
    not_recovered_reason: NotRecoveredReason | None
    line_securement_status: LineSecurementStatus
    secured_quantity: int
    recovered_quantity: int
    expected_delivery_at: datetime | None
    source_warehouse_id: uuid.UUID | None
    supply_source: SupplySource | None


class ResponseOption(CamelModel):
    """
    대응안 비교 1행 (response_options)
      - 주문 집계(fully·delayInduced·itemSecuredBlocked·notRecovered)는 판정 불가·사전 조건 실패면 null(집계 NULL 규칙 G-1~G-3).
      - 화면 "위험 주문"(남은 위험) = notRecoveredOrders. 비용 필드는 없다(U1).
    """

    response_option_id: uuid.UUID
    option_type: OptionType
    feasibility_status: FeasibilityStatus
    reason_code: OptionReasonCode | None
    reason: str | None
    required_quantity: int
    securable_quantity: int | None
    reduction_committed_total: int | None
    expected_delivery_date: date | None
    fully_recovered_orders: int | None
    delay_induced_recovered_orders: int | None
    item_secured_blocked_orders: int | None
    not_recovered_orders: int | None
    recommendation_rank: int | None
    is_recommended: bool
    sku_results: SkuResults
    outcomes: list[OptionOrderOutcome]


class MemberDraftSummary(CamelModel):
    member_draft_id: uuid.UUID
    user_id: uuid.UUID
    display_name: str
    response_option_id: uuid.UUID | None
    opinion_text: str | None
    created_at: datetime


class FinalDecisionSummary(CamelModel):
    final_decision_id: uuid.UUID
    user_id: uuid.UUID
    display_name: str
    response_option_id: uuid.UUID
    reason_text: str
    decided_at: datetime
    checked_seq: int


class AnalysisRunDetailResponse(CamelModel):
    """분석 실행 상세 (GET /analysis-runs/{runId}). 상태별로 비어 있는 묶음은 명세 4.5 '결과 분류와 상태별로 보이는 내용'을 따른다."""

    # 실행
    run_id: uuid.UUID
    run_code: str | None = Field(
        description="표시용 실행 ID (D14, 열 추가 전에는 null)"
    )
    version_no: int
    parent_run_id: uuid.UUID | None
    is_latest: bool
    status: AnalysisRunStatus
    completed_status: CompletedStatus | None
    result_class: ResultClass | None
    status_reason: str | None
    trigger_type: TriggerType
    trigger_reason: TriggerReason
    requested_by: uuid.UUID | None
    request_reason: str | None
    requested_at: datetime | None
    started_at: datetime
    finished_at: datetime | None

    # STALE (status = STALE 일 때만 값이 있다, D23)
    stale_detected_at: datetime | None
    stale_primary_reason: StaleReason | None
    stale_reason_codes: list[StaleReasonCode] | None

    target: AnalysisTarget
    input_basis: InputBasis

    # 결과 (NO_DELAY·ON_HOLD·도구 실패 FAILED 는 빈 목록. 진행 중에 STALE 이 된 실행은 그때까지 저장된 결과만)
    inventory_impacts: list[InventoryImpact]
    at_risk_orders: list[AtRiskOrder]
    response_options: list[ResponseOption]

    # AI 설명 (explanationValidationStatus = PASSED 일 때만 explanation 이 있다)
    explanation: Explanation | None
    explanation_validation_status: ExplanationValidationStatus | None
    explanation_generated_at: datetime | None
    model_name: str | None
    prompt_version: str | None

    # 실패 요약 (ON_HOLD·FAILED 일 때만 값이 있다)
    failed_step: FailedStep | None
    failure_code: str | None
    attempt_count: int | None
    retry_exhausted: bool | None
    failure_message: str | None

    # 판단 이력
    drafts: list[MemberDraftSummary]
    final_decision: FinalDecisionSummary | None
