import uuid
from datetime import date, datetime

from pydantic import Field

from app.core.schemas import CamelModel
from app.modules.delay_analysis.application.delay_event_query_service import (
    DelayEventDetail,
    DelayEventList,
    DelayEventListItem,
)

"""
    지연 목록·상세 응답 (API 15·16, API 명세 4.4절)
      - "3pl" 이 들어간 필드는 to_camel 이 "3Pl" 로 바꾸므로 명세 이름(planned3plInboundDate 등)을 별칭으로 직접 줍니다.
      - runCode(D14)는 analysis_runs.run_code 마이그레이션 전이라 항상 null 입니다.
"""

_PLANNED = "planned3plInboundDate"
_EXPECTED = "expected3plInboundDate"
_PREVIOUS_EXPECTED = "previousExpected3plInboundDate"


class DelayEventCountsResponse(CamelModel):
    """탭별 건수. 탭·최종 선택 여부 필터를 뺀 나머지 필터를 같게 적용합니다."""

    all: int = Field(description="전체")
    pending_decision: int = Field(description="선택 대기 (READY_FOR_REVIEW)")
    analyzing: int = Field(description="분석 중 (DETECTED~VALIDATING_OUTPUT)")
    on_hold: int = Field(description="분석 보류 (ON_HOLD)")
    needs_reanalysis: int = Field(description="재분석 필요 (STALE·FAILED)")


class DelayEventListItemResponse(CamelModel):
    delay_event_id: uuid.UUID
    shipment_code: str
    representative_sku: str | None = Field(
        description="대표 상품명: 선적에서 수량이 가장 많은 SKU (같으면 상세 줄 번호가 작은 SKU)"
    )
    representative_sku_code: str | None
    sku_count: int
    route: str = Field(examples=["KR → LOS ANGELES"])
    stage: str
    planned_3pl_inbound_date: date = Field(alias=_PLANNED)
    expected_3pl_inbound_date: date = Field(alias=_EXPECTED)
    delay_days: int = Field(description="예상 3PL 입고일 − 계획 3PL 입고일 (일)")
    detected_at: datetime
    at_risk_order_count: int | None = Field(
        description="현재 실행의 위험 주문 수. 실행이 없거나 분석 중이면 null"
    )
    run_id: uuid.UUID | None = Field(description="현재 실행(is_latest) ID")
    run_code: str | None = Field(description="표시용 실행 ID (D14, 아직 null)")
    run_status: str | None
    has_final_decision: bool

    @classmethod
    def of(cls, item: DelayEventListItem) -> DelayEventListItemResponse:
        row = item.row
        return cls(
            delay_event_id=row.delay_event_id,
            shipment_code=row.shipment_code,
            representative_sku=item.shipment.representative_sku,
            representative_sku_code=item.shipment.representative_sku_code,
            sku_count=item.shipment.sku_count,
            route=item.shipment.route,
            stage=row.stage,
            planned_3pl_inbound_date=row.planned_3pl_inbound_date,
            expected_3pl_inbound_date=row.expected_3pl_inbound_date,
            delay_days=item.delay_days,
            detected_at=row.detected_at,
            at_risk_order_count=row.at_risk_order_count,
            run_id=row.run_id,
            run_code=None,
            run_status=row.run_status,
            has_final_decision=row.has_final_decision,
        )


class DelayEventListResponse(CamelModel):
    items: list[DelayEventListItemResponse]
    counts: DelayEventCountsResponse

    @classmethod
    def of(cls, result: DelayEventList) -> DelayEventListResponse:
        counts = result.counts
        return cls(
            items=[DelayEventListItemResponse.of(item) for item in result.items],
            counts=DelayEventCountsResponse(
                all=counts.all_count,
                pending_decision=counts.pending_decision,
                analyzing=counts.analyzing,
                on_hold=counts.on_hold,
                needs_reanalysis=counts.needs_reanalysis,
            ),
        )


class DelayEventShipmentResponse(CamelModel):
    shipment_id: uuid.UUID
    shipment_code: str
    transport_mode: str
    warehouse_id: uuid.UUID
    route: str
    representative_sku: str | None
    representative_sku_code: str | None
    sku_count: int


class AnalysisRunHistoryResponse(CamelModel):
    run_id: uuid.UUID
    run_code: str | None = Field(description="표시용 실행 ID (D14, 아직 null)")
    version_no: int
    status: str
    is_latest: bool
    trigger_type: str
    started_at: datetime
    finished_at: datetime | None


class DelayEventDetailResponse(CamelModel):
    delay_event_id: uuid.UUID
    shipment: DelayEventShipmentResponse
    stage: str
    planned_3pl_inbound_date: date = Field(alias=_PLANNED)
    previous_expected_3pl_inbound_date: date | None = Field(
        alias=_PREVIOUS_EXPECTED, description="이전 관측의 예상 3PL 입고일"
    )
    expected_3pl_inbound_date: date = Field(alias=_EXPECTED)
    delay_days: int
    detected_at: datetime
    closed_at: datetime | None = Field(
        description="지연 에피소드 종료 시각 (회복·입고). 열려 있으면 null"
    )
    runs: list[AnalysisRunHistoryResponse] = Field(
        description="분석 실행 버전 이력 (최신 버전 먼저)"
    )

    @classmethod
    def of(cls, detail: DelayEventDetail) -> DelayEventDetailResponse:
        row = detail.row
        return cls(
            delay_event_id=row.delay_event_id,
            shipment=DelayEventShipmentResponse(
                shipment_id=row.shipment_id,
                shipment_code=row.shipment_code,
                transport_mode=row.transport_mode,
                warehouse_id=row.warehouse_id,
                route=detail.shipment.route,
                representative_sku=detail.shipment.representative_sku,
                representative_sku_code=detail.shipment.representative_sku_code,
                sku_count=detail.shipment.sku_count,
            ),
            stage=row.stage,
            planned_3pl_inbound_date=row.planned_3pl_inbound_date,
            previous_expected_3pl_inbound_date=detail.previous_expected_3pl_inbound_date,
            expected_3pl_inbound_date=row.expected_3pl_inbound_date,
            delay_days=detail.delay_days,
            detected_at=row.detected_at,
            closed_at=row.closed_at,
            runs=[
                AnalysisRunHistoryResponse(
                    run_id=run.analysis_run_id,
                    run_code=None,
                    version_no=run.version_no,
                    status=run.status,
                    is_latest=run.is_latest,
                    trigger_type=run.trigger_type,
                    started_at=run.started_at,
                    finished_at=run.finished_at,
                )
                for run in detail.runs
            ],
        )
