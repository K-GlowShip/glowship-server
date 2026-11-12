import logging
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import (
    Field,
    PlainSerializer,
    StrictInt,
    StrictStr,
    ValidationError,
)

from app.core.schemas import CamelModel
from app.core.summary_schemas import (
    ResponseOptionSummary,
    ShipmentSummary,
    SkuSummary,
    UserSummary,
    WarehouseSummary,
)
from app.modules.delay_analysis.application.analysis_run_detail_service import (
    AnalysisRunDetail,
    AnalysisTargetDetail,
)
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
from app.modules.delay_analysis.infrastructure.repository.analysis_run_detail_repository import (
    AtRiskLineRow,
    FinalDecisionRow,
    InventoryImpactRow,
    OptionOutcomeRow,
    ResponseOptionRow,
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

logger = logging.getLogger(__name__)

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
    """
    분석을 시작할 때 정한 데이터 경고(이슈 #122). 원천 하나에 하나.
      - lastStatus = FAILED 면 그 원천의 마지막으로 끝난 수집이 실패
      - 그 밖(SUCCEEDED·PARTIAL_SUCCESS)이면 수집은 됐지만 자료가 실행 날짜까지 없어 기준일 D 를 당김(재고·주문)
    """

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
    """
    SKU·창고별 재고 영향. 기준선(baseline_*) = 계획 입고일에 들어왔을 때, 전체 = 예상 입고일 반영.
      - SKU·창고는 요약 객체(sku·warehouse)다. 명세 2절 "연관 대상"(113행): 한글 항목 "SKU"·"창고"는 요약 객체로 준다.
    """

    sku: SkuSummary
    warehouse: WarehouseSummary
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
    sku: SkuSummary = Field(description="주문 줄의 SKU 요약 (명세 2절 연관 대상)")
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
    is_beyond_horizon: bool = Field(
        description="예상 납품이 계산 범위(horizon) 밖인지 (at_risk_orders.expected_beyond_horizon)"
    )
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
    source_warehouse: WarehouseSummary | None = Field(
        description="공급 창고 요약. 공급 창고가 없으면 null"
    )
    supply_source: SupplySource | None


class ResponseOption(CamelModel):
    """
    대응안 비교 1행 (response_options)
      - 주문 집계(fully·delayInduced·itemSecuredBlocked·notRecovered)는 판정 불가·사전 조건 실패면 null(집계 NULL 규칙 G-1~G-3).
      - 화면 "위험 주문"(남은 위험) = notRecoveredOrders. 비용 필드는 없다(U1).
      - response_options.reason 은 넣지 않는다. 저장기가 reason_code 와 같은 값을 넣고, 화면 문구는 reasonCode 라벨로 만든다(명세 4.5).
    """

    response_option_id: uuid.UUID
    option_type: OptionType
    feasibility_status: FeasibilityStatus
    reason_code: OptionReasonCode | None
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
    sku_results: SkuResults | None = Field(
        description="sku_results.v1 저장 JSON 그대로(D20). 저장값이 계약 모델로 읽히지 않으면 null 이고 서버가 오류 로그를 남긴다"
    )
    outcomes: list[OptionOrderOutcome]


class MemberDraftSummary(CamelModel):
    """팀원 의견·선택 초안 (member_drafts). 작성자·대응안은 요약 객체(응답 본문 규칙 5.2)"""

    member_draft_id: uuid.UUID
    author: UserSummary
    response_option: ResponseOptionSummary | None = Field(
        description="초안이 고른 대응안. 의견만 남겼으면 null"
    )
    opinion_text: str | None
    created_at: datetime


class FinalDecisionSummary(CamelModel):
    """팀장 최종 선택 (final_decisions). 선택한 사람·대응안은 요약 객체(응답 본문 규칙 5.2)"""

    final_decision_id: uuid.UUID
    decided_by: UserSummary
    response_option: ResponseOptionSummary
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
    requested_by: UserSummary | None = Field(
        description="재분석을 요청한 사용자 요약. 자동 실행(AUTO)이면 null"
    )
    request_reason: str | None
    requested_at: datetime | None
    started_at: datetime
    finished_at: datetime | None

    # STALE (status = STALE 일 때만 값이 있다, D23). STALE 이 아니면 staleReasonCodes 는 null 이 아니라 [] (명세 4.5)
    stale_detected_at: datetime | None
    stale_primary_reason: StaleReason | None
    stale_reason_codes: list[StaleReasonCode]

    target: AnalysisTarget
    input_basis: InputBasis

    # 결과 (NO_DELAY·ON_HOLD·도구 실패 FAILED 는 빈 목록. 진행 중에 STALE 이 된 실행은 그때까지 저장된 결과만)
    inventory_impacts: list[InventoryImpact]
    at_risk_orders: list[AtRiskOrder]
    response_options: list[ResponseOption]

    # AI 설명 (explanationValidationStatus = PASSED 일 때만 explanation 이 있다)
    explanation: Explanation | None = Field(
        description="explanation.v1 저장 JSON 그대로(D20). 설명 검증을 통과한 실행에만 있다. "
        "저장값이 계약 모델로 읽히지 않으면(계약 변경 등) 상세 전체를 실패시키지 않고 이 필드만 null 로 두며 서버가 오류 로그를 남긴다"
    )
    explanation_validation_status: ExplanationValidationStatus | None
    explanation_generated_at: datetime | None
    model_name: str | None
    prompt_version: str | None

    # 실패 요약 (ON_HOLD·FAILED 일 때만 값이 있다)
    failed_step: FailedStep | None
    failure_code: str | None
    attempt_count: int | None
    is_retry_exhausted: bool | None = Field(
        description="재시도를 모두 썼는지 (analysis_runs.retry_exhausted)"
    )
    failure_message: str | None

    # 판단 이력
    drafts: list[MemberDraftSummary]
    final_decision: FinalDecisionSummary | None

    @classmethod
    def of(cls, detail: AnalysisRunDetail) -> AnalysisRunDetailResponse:
        run = detail.run
        return cls(
            run_id=run.analysis_run_id,
            run_code=run.run_code,
            version_no=run.version_no,
            parent_run_id=run.parent_analysis_run_id,
            is_latest=run.is_latest,
            status=run.status,
            completed_status=run.completed_status,
            result_class=run.result_class,
            status_reason=run.status_reason,
            trigger_type=run.trigger_type,
            trigger_reason=run.trigger_reason,
            requested_by=UserSummary(
                user_id=run.requested_by_user_id,
                display_name=run.requested_by_display_name,
                role_code=run.requested_by_role_code,
            )
            if run.requested_by_user_id is not None
            else None,
            request_reason=run.request_reason,
            requested_at=run.requested_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
            stale_detected_at=run.stale_detected_at,
            stale_primary_reason=run.stale_primary_reason,
            stale_reason_codes=run.stale_reason_codes or [],
            target=_target(detail.target),
            input_basis=InputBasis(
                input_seq=run.input_seq,
                base_date=run.base_date,
                operating_rule_set_id=run.operating_rule_set_id,
                operating_rule_version_no=run.operating_rule_version_no,
                calculation_contract_version=run.calculation_contract_version,
                allocation_rule_version=run.allocation_rule_version,
                data_versions=[
                    DataVersion(
                        data_sync_run_id=v.data_sync_run_id,
                        source_domain=v.source_domain,
                        finished_at=v.finished_at,
                    )
                    for v in detail.data_versions
                ],
                data_basis_at=detail.data_basis_at,
                data_warnings=[
                    DataWarning(
                        source_domain=w["source_domain"],
                        last_status=w["last_status"],
                        last_finished_at=w.get("last_finished_at"),
                    )
                    for w in sorted(
                        run.data_warnings or [], key=lambda w: w["source_domain"]
                    )
                ],
            ),
            inventory_impacts=[_impact(row) for row in detail.inventory_impacts],
            at_risk_orders=_group_at_risk_orders(
                detail.at_risk_lines, run.analysis_run_id
            ),
            response_options=_response_options(
                detail.response_options, detail.option_outcomes
            ),
            explanation=_stored_json(
                Explanation,
                run.final_explanation_json,
                column="analysis_runs.final_explanation_json",
                row_id=run.analysis_run_id,
            ),
            explanation_validation_status=run.explanation_validation_status,
            explanation_generated_at=run.explanation_generated_at,
            model_name=run.model_name,
            prompt_version=run.prompt_version,
            failed_step=run.failed_step,
            failure_code=run.failure_code,
            attempt_count=run.attempt_count,
            is_retry_exhausted=run.retry_exhausted,
            failure_message=run.failure_message,
            drafts=[
                MemberDraftSummary(
                    member_draft_id=d.member_draft_id,
                    author=UserSummary(
                        user_id=d.user_id,
                        display_name=d.display_name,
                        role_code=d.role_code,
                    ),
                    response_option=ResponseOptionSummary(
                        response_option_id=d.response_option_id,
                        option_type=d.option_type,
                    )
                    if d.response_option_id is not None
                    else None,
                    opinion_text=d.opinion_text,
                    created_at=d.created_at,
                )
                for d in detail.member_drafts
            ],
            final_decision=_final_decision(detail.final_decision),
        )


# --- 저장 행 → 응답 변환 -----------------------------------------------------------------------


def _target(target: AnalysisTargetDetail) -> AnalysisTarget:
    """지연 목록·상세(4.4)와 같은 요약 객체·표시값 (D13)"""
    row = target.row
    line = target.shipment.representative_line
    return AnalysisTarget(
        delay_event_id=row.delay_event_id,
        episode_no=row.episode_no,
        shipment=ShipmentSummary(
            shipment_id=row.shipment_id, shipment_code=row.shipment_code
        ),
        warehouse=WarehouseSummary(
            warehouse_id=row.warehouse_id,
            warehouse_code=row.warehouse_code,
            region_code=row.region_code,
        ),
        representative_sku=SkuSummary(
            sku_id=line.sku_id, sku_code=line.sku_code, product_name=line.product_name
        )
        if line
        else None,
        route=target.shipment.route,
        stage=row.stage,
        planned_3pl_inbound_date=row.planned_3pl_inbound_date,
        expected_3pl_inbound_date=row.expected_3pl_inbound_date,
        delay_days=target.delay_days,
    )


def _impact(row: InventoryImpactRow) -> InventoryImpact:
    """재고 영향 1행. SKU·창고는 요약 객체로, 나머지 열은 이름 그대로 옮긴다."""
    values = {
        name: getattr(row, name)
        for name in InventoryImpact.model_fields
        if name not in ("sku", "warehouse")
    }
    return InventoryImpact(
        sku=SkuSummary(
            sku_id=row.sku_id, sku_code=row.sku_code, product_name=row.product_name
        ),
        warehouse=WarehouseSummary(
            warehouse_id=row.warehouse_id,
            warehouse_code=row.warehouse_code,
            region_code=row.region_code,
        ),
        **values,
    )


def _stored_json[M: StoredJsonModel](
    model: type[M], value: dict | None, *, column: str, row_id: uuid.UUID
) -> M | None:
    """
    저장 JSON(explanation.v1·sku_results.v1)을 계약 모델로 읽는다. 저장값이 없으면 None.
    계약이 바뀌었거나 저장값이 모델과 맞지 않으면 상세 조회 전체를 500으로 만들지 않고
    그 필드만 None 으로 두고 오류 로그를 남긴다(PR #32 리뷰 [should], 선택지 "검증 실패 시 null + 로그").
    """
    if value is None:
        return None
    try:
        return model.model_validate(value)
    except ValidationError as error:
        logger.error(
            "저장 JSON 이 계약 모델과 맞지 않아 응답에서 null 로 둠: column=%s id=%s errors=%s",
            column,
            row_id,
            error.error_count(),
        )
        return None


def _order_level_values(line: AtRiskLineRow) -> tuple:
    """주문 단위로 같아야 하는 값(주문 묶음의 귀속·약속·예상 납품·horizon 밖 여부·사유)"""
    return (
        line.risk_attribution,
        line.promised_delivery_at,
        line.baseline_expected_delivery_at,
        line.expected_delivery_at,
        line.expected_beyond_horizon,
        line.risk_reason,
    )


def _group_at_risk_orders(
    lines: list[AtRiskLineRow], run_id: uuid.UUID
) -> list[AtRiskOrder]:
    """
    at_risk_orders 는 주문 상세 1줄이 1행이다. 주문별로 묶고 상세는 lines[] 에 넣는다(D21, U12).
    주문 단위 값(귀속·약속·예상 납품·horizon 밖 여부·사유)은 저장기가 주문의 모든 줄에 같게 넣는다(2026 H1 데이터 확인).
    저장 순서(주문 코드·줄 번호)대로 받으므로, 주문 단위 값은 그 주문의 첫 줄(가장 작은 줄 번호)에서 읽는다.
    DDL 이 이것을 보장하지는 않으므로, 줄끼리 값이 다르면 첫 줄 값을 쓰고 경고 로그를 남긴다(PR #32 리뷰).
    """
    orders: dict[uuid.UUID, AtRiskOrder] = {}
    first_values: dict[uuid.UUID, tuple] = {}
    for line in lines:
        order = orders.get(line.order_id)
        values = _order_level_values(line)
        if order is not None and values != first_values[line.order_id]:
            logger.warning(
                "at_risk_orders 주문 단위 값이 줄마다 다름(첫 줄 값을 씀): "
                "analysis_run_id=%s order_id=%s at_risk_order_id=%s line_no=%s",
                run_id,
                line.order_id,
                line.at_risk_order_id,
                line.line_no,
            )
        if order is None:
            first_values[line.order_id] = values
            order = orders[line.order_id] = AtRiskOrder(
                order_id=line.order_id,
                order_code=line.order_code,
                risk_attribution=line.risk_attribution,
                promised_delivery_at=line.promised_delivery_at,
                baseline_expected_delivery_at=line.baseline_expected_delivery_at,
                expected_delivery_at=line.expected_delivery_at,
                is_beyond_horizon=line.expected_beyond_horizon,
                risk_reason=line.risk_reason,
                lines=[],
            )
        order.lines.append(
            AtRiskOrderLine(
                at_risk_order_id=line.at_risk_order_id,
                order_line_id=line.order_line_id,
                line_no=line.line_no,
                sku=SkuSummary(
                    sku_id=line.sku_id,
                    sku_code=line.sku_code,
                    product_name=line.product_name,
                ),
                open_quantity=line.open_quantity,
                wait_cause=line.wait_cause,
            )
        )
    return list(orders.values())


def _response_options(
    options: list[ResponseOptionRow], outcomes: list[OptionOutcomeRow]
) -> list[ResponseOption]:
    """대응안마다 저장된 주문 상세 결과(outcomes[])를 붙인다. 공급 창고는 창고 요약 객체로 바꾼다."""
    by_option: dict[uuid.UUID, list[OptionOrderOutcome]] = {
        o.response_option_id: [] for o in options
    }
    for x in outcomes:
        by_option[x.response_option_id].append(
            OptionOrderOutcome(
                at_risk_order_id=x.at_risk_order_id,
                outcome=x.outcome,
                not_recovered_reason=x.not_recovered_reason,
                line_securement_status=x.line_securement_status,
                secured_quantity=x.secured_quantity,
                recovered_quantity=x.recovered_quantity,
                expected_delivery_at=x.expected_delivery_at,
                source_warehouse=WarehouseSummary(
                    warehouse_id=x.source_warehouse_id,
                    warehouse_code=x.source_warehouse_code,
                    region_code=x.source_region_code,
                )
                if x.source_warehouse_id is not None
                else None,
                supply_source=x.supply_source,
            )
        )
    return [
        ResponseOption(
            response_option_id=o.response_option_id,
            option_type=o.option_type,
            feasibility_status=o.feasibility_status,
            reason_code=o.reason_code,
            required_quantity=o.required_quantity,
            securable_quantity=o.securable_quantity,
            reduction_committed_total=o.reduction_committed_total,
            expected_delivery_date=o.expected_delivery_date,
            fully_recovered_orders=o.fully_recovered_orders,
            delay_induced_recovered_orders=o.delay_induced_recovered_orders,
            item_secured_blocked_orders=o.item_secured_blocked_orders,
            not_recovered_orders=o.not_recovered_orders,
            recommendation_rank=o.recommendation_rank,
            is_recommended=o.is_recommended,
            sku_results=_stored_json(
                SkuResults,
                o.sku_results,
                column="response_options.sku_results",
                row_id=o.response_option_id,
            ),
            outcomes=by_option[o.response_option_id],
        )
        for o in options
    ]


def _final_decision(row: FinalDecisionRow | None) -> FinalDecisionSummary | None:
    if row is None:
        return None
    return FinalDecisionSummary(
        final_decision_id=row.final_decision_id,
        decided_by=UserSummary(
            user_id=row.user_id, display_name=row.display_name, role_code=row.role_code
        ),
        response_option=ResponseOptionSummary(
            response_option_id=row.response_option_id, option_type=row.option_type
        ),
        reason_text=row.reason_text,
        decided_at=row.decided_at,
        checked_seq=row.checked_seq,
    )
