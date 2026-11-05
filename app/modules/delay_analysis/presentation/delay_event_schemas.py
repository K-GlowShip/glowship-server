import uuid
from datetime import date, datetime

from pydantic import Field

from app.core.pagination import Page
from app.core.schemas import CamelModel
from app.core.summary_schemas import (
    RunSummary,
    ShipmentSummary,
    SkuSummary,
    WarehouseSummary,
)
from app.modules.delay_analysis.application.delay_event_query_service import (
    DelayEventDetail,
    DelayEventList,
    DelayEventListItem,
    ShipmentDisplay,
)
from app.modules.delay_analysis.infrastructure.repository.delay_event_read_repository import (
    DelayEventRow,
)
from app.modules.delay_analysis.presentation.analysis_run_schemas import (
    EXPECTED_3PL,
    PLANNED_3PL,
)

"""
    지연 목록·상세 응답 (API 15·16, API 명세 4.4절, 응답 본문 규칙)
      - 연관 대상은 요약 객체(shipment·warehouse·representativeSku·currentRun)로 줍니다 (app/core/summary_schemas).
      - "3pl" 이 들어간 필드는 to_camel 이 "3Pl" 로 바꾸므로 명세 이름(planned3plInboundDate 등)을 별칭으로 직접 줍니다.
        계획·예상 입고일 별칭은 분석 실행 응답(analysis_run_schemas)과 같은 상수를 씁니다.
      - episodeNo, currentRun.resultClass, runs[].triggerReason·completedStatus·resultClass 는 0005 열에서 읽습니다.
      - 아직 열이 없어 null 로 주는 필드: currentRun.runCode, runs[].runCode → 0006 (D14)
"""

_PLANNED = PLANNED_3PL
_EXPECTED = EXPECTED_3PL
_PREVIOUS_EXPECTED = "previousExpected3plInboundDate"


class DelayEventCountsResponse(CamelModel):
    """탭별 건수. 키는 탭(실행 상태) 이름의 camelCase 이고, 탭·최종 선택 여부 필터를 뺀 나머지 필터를 같게 적용합니다."""

    all: int = Field(description="전체")
    ready_for_review: int = Field(description="선택 대기 (READY_FOR_REVIEW)")
    analyzing: int = Field(description="분석 중 (DETECTED~VALIDATING_OUTPUT)")
    on_hold: int = Field(description="분석 보류 (ON_HOLD)")
    needs_reanalysis: int = Field(description="재분석 필요 (STALE·FAILED)")


class _DelayEventBase(CamelModel):
    """지연 목록 행과 상세가 함께 쓰는 필드. 단계·입고일·지연 일수는 선적의 현재 값입니다."""

    delay_event_id: uuid.UUID
    episode_no: int = Field(description="선적별 지연 에피소드 번호 (1부터)")
    shipment: ShipmentSummary
    warehouse: WarehouseSummary = Field(description="도착 창고")
    representative_sku: SkuSummary | None = Field(
        description="선적에서 수량이 가장 많은 SKU (같으면 상세 줄 번호가 작은 SKU, 상세가 없으면 null)"
    )
    sku_count: int
    route: str = Field(examples=["KR → LOS ANGELES"])
    transport_mode: str = Field(description="OCEAN·AIR")
    stage: str = Field(description="선적의 현재 운송 단계")
    planned_3pl_inbound_date: date = Field(alias=_PLANNED)
    expected_3pl_inbound_date: date = Field(
        alias=_EXPECTED, description="선적의 현재 예상 3PL 입고일"
    )
    delay_days: int = Field(
        description="예상 3PL 입고일 − 계획 3PL 입고일 (일). 회복되어 예상일 ≤ 계획일이면 0"
    )
    detected_at: datetime
    closed_at: datetime | None = Field(
        description="지연 에피소드 종료 시각. 열려 있으면 null"
    )
    close_reason: str | None = Field(
        description="종료 사유 RECOVERED·RECEIVED. 열려 있으면 null"
    )

    @staticmethod
    def _base_fields(
        row: DelayEventRow, shipment: ShipmentDisplay, delay_days: int
    ) -> dict:
        line = shipment.representative_line
        return {
            "delay_event_id": row.delay_event_id,
            "episode_no": row.episode_no,
            "shipment": ShipmentSummary(
                shipment_id=row.shipment_id, shipment_code=row.shipment_code
            ),
            "warehouse": WarehouseSummary(
                warehouse_id=row.warehouse_id,
                warehouse_code=row.warehouse_code,
                region_code=row.region_code,
            ),
            "representative_sku": SkuSummary(
                sku_id=line.sku_id,
                sku_code=line.sku_code,
                product_name=line.product_name,
            )
            if line
            else None,
            "sku_count": shipment.sku_count,
            "route": shipment.route,
            "transport_mode": row.transport_mode,
            "stage": row.stage,
            "planned_3pl_inbound_date": row.planned_3pl_inbound_date,
            "expected_3pl_inbound_date": row.expected_3pl_inbound_date,
            "delay_days": delay_days,
            "detected_at": row.detected_at,
            "closed_at": row.closed_at,
            "close_reason": row.close_reason,
        }


class DelayEventListItemResponse(_DelayEventBase):
    at_risk_order_count: int | None = Field(
        description=(
            "현재 실행의 위험 주문 수(서로 다른 주문 수, D21). 저장된 계산 결과가 없으면 null: "
            "실행 없음·분석 중·ON_HOLD·NO_DELAY·도구 실패 FAILED"
        )
    )
    current_run: RunSummary | None = Field(
        description="현재 실행(is_latest). 실행이 없으면 null"
    )
    has_final_decision: bool

    @classmethod
    def of(cls, item: DelayEventListItem) -> DelayEventListItemResponse:
        row = item.row
        return cls(
            **cls._base_fields(row, item.shipment, item.delay_days),
            at_risk_order_count=row.at_risk_order_count,
            current_run=RunSummary(
                run_id=row.run_id,
                run_code=None,
                status=row.run_status,
                result_class=row.run_result_class,
                failure_code=row.run_failure_code,
            )
            if row.run_id
            else None,
            has_final_decision=row.has_final_decision,
        )


class DelayEventListResponse(Page[DelayEventListItemResponse]):
    """공통 목록 응답(items·page·size·totalCount·totalPages)에 탭별 건수를 더합니다."""

    counts: DelayEventCountsResponse = Field(
        description="탭별 건수. 탭·hasFinalDecision 필터를 뺀 값이라 totalCount 와 다를 수 있습니다"
    )

    @classmethod
    def from_result(cls, result: DelayEventList) -> DelayEventListResponse:
        counts = result.counts
        return cls.of(
            [DelayEventListItemResponse.of(item) for item in result.items],
            result.total_count,
            result.page,
            counts=DelayEventCountsResponse(
                all=counts.all_count,
                ready_for_review=counts.ready_for_review,
                analyzing=counts.analyzing,
                on_hold=counts.on_hold,
                needs_reanalysis=counts.needs_reanalysis,
            ),
        )


class AnalysisRunHistoryResponse(CamelModel):
    run_id: uuid.UUID
    run_code: str | None = Field(
        description="표시용 실행 ID (D14). 0006 병합 전에는 null"
    )
    version_no: int
    status: str
    is_latest: bool
    trigger_type: str = Field(description="AUTO·REANALYSIS")
    trigger_reason: str = Field(
        description="실행 생성 사유 (analysis_runs_ck15, 명세 3절 실행 생성 경로)"
    )
    parent_run_id: uuid.UUID | None = Field(
        description="이전 버전 실행. 1번째 버전이면 null"
    )
    completed_status: str | None = Field(
        description="완료 판정 당시 상태. 완료 전(진행 중에 STALE 이 된 실행 포함)이면 null"
    )
    result_class: str | None = Field(
        description="결과 분류. 완료 판정이 READY_FOR_REVIEW·NO_RISK 일 때만 값이 있고, 완료 전·NO_DELAY·ON_HOLD·FAILED 이면 null (DDL ck21·ck22)"
    )
    stale_detected_at: datetime | None = Field(
        description="STALE 기록 시각. STALE 이 아니면 null"
    )
    stale_primary_reason: str | None = Field(
        description="STALE 대표 사유. STALE 이 아니면 null"
    )
    started_at: datetime
    finished_at: datetime | None


class DelayEventDetailResponse(_DelayEventBase):
    previous_expected_3pl_inbound_date: date | None = Field(
        alias=_PREVIOUS_EXPECTED,
        description="감지 직전 관측의 예상 3PL 입고일 (스냅샷). 감지 당시 맥락을 보여 줄 때 씁니다",
    )
    runs: list[AnalysisRunHistoryResponse] = Field(
        description="분석 실행 버전 이력 (최신 버전 먼저)"
    )

    @classmethod
    def of(cls, detail: DelayEventDetail) -> DelayEventDetailResponse:
        return cls(
            **cls._base_fields(detail.row, detail.shipment, detail.delay_days),
            previous_expected_3pl_inbound_date=detail.previous_expected_3pl_inbound_date,
            runs=[
                AnalysisRunHistoryResponse(
                    run_id=run.analysis_run_id,
                    run_code=None,
                    version_no=run.version_no,
                    status=run.status,
                    is_latest=run.is_latest,
                    trigger_type=run.trigger_type,
                    trigger_reason=run.trigger_reason,
                    parent_run_id=run.parent_analysis_run_id,
                    completed_status=run.completed_status,
                    result_class=run.result_class,
                    stale_detected_at=run.stale_detected_at,
                    stale_primary_reason=run.stale_primary_reason,
                    started_at=run.started_at,
                    finished_at=run.finished_at,
                )
                for run in detail.runs
            ],
        )
