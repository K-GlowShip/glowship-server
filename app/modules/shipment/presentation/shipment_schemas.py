import uuid
from datetime import date, datetime

from pydantic import Field

from app.core.pagination import Page
from app.core.schemas import CamelModel
from app.core.summary_schemas import SkuSummary, WarehouseSummary
from app.modules.delay_analysis.presentation.analysis_run_schemas import (
    EXPECTED_3PL,
    PLANNED_3PL,
    JsonNumberDecimal,
)
from app.modules.shipment.application.shipment_detail_service import (
    ShipmentDetail,
)
from app.modules.shipment.application.shipment_list_service import (
    ShipmentListItem,
    ShipmentListResult,
)
from app.modules.shipment.infrastructure.shipment_enums import (
    ShipmentStage,
    TransportMode,
)

MAX_QUERY_LENGTH = 100
# to_camel 은 "3pl" 다음 글자를 대문자로 만든다(actual3PlInboundDate). 명세 표기로 별칭을 고정한다 (지연 목록과 같음).
ACTUAL_3PL = "actual3plInboundDate"


class ShipmentListItemResponse(CamelModel):
    """선적 탭의 한 행 (명세 4.3 GET /shipments).
    route·representativeSku·skuCount·delayDays 는 명세 4.3 에 없고 지연 목록(4.4)과 같은 계산(D13)으로 더한 필드다.
    stage·expected3plInboundDate 는 선적의 현재 값이다."""

    shipment_id: uuid.UUID
    shipment_code: str = Field(
        description="화면의 선적 코드 (shipments.shipment_code)",
        examples=["OCN-20260601"],
    )
    transport_mode: TransportMode = Field(
        description="OCEAN(해상)·AIR(항공)", examples=["OCEAN"]
    )
    route: str = Field(
        description=(
            "경로 표시 문자열 (D13). 한국 출고 창고 지역 → 도착 항구 이름. "
            "지연 목록의 route 와 같은 계산"
        ),
        examples=["KR → LOS ANGELES"],
    )
    warehouse: WarehouseSummary = Field(description="도착 창고 (미국 3PL)")
    representative_sku: SkuSummary | None = Field(
        description=(
            "대표 상품. 선적에서 수량이 가장 많은 SKU, 같으면 상세 줄 번호가 작은 SKU (D13). "
            "상세 줄이 없으면 null"
        )
    )
    sku_count: int = Field(
        description="선적에 실린 서로 다른 SKU 수. 상세 줄이 없으면 0", examples=[20]
    )
    stage: ShipmentStage = Field(
        description=(
            "현재 운송 단계 (shipments.current_stage): "
            "PRE_DEPARTURE(출항 전)·AT_SEA(해상 운송)·IN_AIR(항공 운송)·AT_PORT(항구 도착·통관 중)·RECEIVED(입고 완료)"
        ),
        examples=["AT_SEA"],
    )
    planned_kr_release_date: date = Field(
        description="한국 창고 계획 출고일", examples=["2026-06-17"]
    )
    planned_3pl_inbound_date: date = Field(
        alias=PLANNED_3PL,
        description=(
            "계획 3PL 입고일. 계획 출고일 + 올림(시장 리드타임 p90)으로 새 선적 적재 때 한 번 정한 값. "
            "목록 정렬 기준(최신순)"
        ),
        examples=["2026-07-12"],
    )
    expected_3pl_inbound_date: date = Field(
        alias=EXPECTED_3PL,
        description=(
            "현재 예상 3PL 입고일 (shipments.current_expected_3pl_inbound_date). "
            "입고 완료 뒤에는 실제 입고일과 같은 값"
        ),
        examples=["2026-07-06"],
    )
    actual_3pl_inbound_date: date | None = Field(
        alias=ACTUAL_3PL,
        description="실제 3PL 입고일. 입고 전이면 null. stage 가 RECEIVED 면 항상 값이 있다 (ERD ck3)",
        examples=[None],
    )
    is_delayed: bool = Field(
        description=(
            "지연 여부 = delayDays > 0. 기준값은 입고 전이면 예상 입고일, 입고 완료면 실제 입고일이고 "
            "그 값이 계획 3PL 입고일보다 늦으면 true (서비스 플로우 1절의 감지·사후 평가 구분). "
            "대시보드 지연 선적(명세 4.4 C22, 열린 에피소드만)과 다른 기준이라 입고 완료 선적의 지연 입고도 true"
        ),
        examples=[False],
    )
    delay_days: int = Field(
        description=(
            "지연 일수 = max(0, (입고 완료면 실제 입고일, 아니면 예상 입고일) − 계획 3PL 입고일). "
            "지연 목록의 delayDays 와 같은 이름·계산. 회복되어 계획일 이내면 0"
        ),
        examples=[0],
    )

    @classmethod
    def of(cls, item: ShipmentListItem) -> ShipmentListItemResponse:
        representative_sku = (
            SkuSummary(
                sku_id=item.representative_sku_id,
                sku_code=item.representative_sku_code,
                product_name=item.representative_product_name,
            )
            if item.representative_sku_id is not None
            else None
        )
        return cls(
            shipment_id=item.shipment_id,
            shipment_code=item.shipment_code,
            transport_mode=item.transport_mode,
            route=item.route,
            warehouse=WarehouseSummary(
                warehouse_id=item.warehouse_id,
                warehouse_code=item.warehouse_code,
                region_code=item.region_code,
            ),
            representative_sku=representative_sku,
            sku_count=item.sku_count,
            stage=item.stage,
            planned_kr_release_date=item.planned_kr_release_date,
            planned_3pl_inbound_date=item.planned_3pl_inbound_date,
            expected_3pl_inbound_date=item.expected_3pl_inbound_date,
            actual_3pl_inbound_date=item.actual_3pl_inbound_date,
            is_delayed=item.is_delayed,
            delay_days=item.delay_days,
        )


class ShipmentListResponse(Page[ShipmentListItemResponse]):
    """GET /api/v1/shipments 응답. 공통 목록 모양(items·page·size·totalCount·totalPages)"""

    @classmethod
    def from_result(cls, result: ShipmentListResult) -> ShipmentListResponse:
        return cls.of(
            [ShipmentListItemResponse.of(item) for item in result.items],
            result.total_count,
            result.page,
        )


class ShipmentLineResponse(CamelModel):
    """선적 상세 한 줄 (shipment_lines)"""

    line_no: int = Field(description="상세 줄 번호(1부터)", examples=[1])
    sku: SkuSummary = Field(description="실린 상품")
    shipped_quantity: int = Field(description="선적 수량", examples=[1200])


class ShipmentPlannedBasisResponse(CamelModel):
    """계획 리드타임 근거 (market_leadtime_references). 계획 3PL 입고일 = 계획 출고일 + 올림(p90)"""

    month: str = Field(description="기준 달 YYYY-MM", examples=["2026-06"])
    us_port_name: str = Field(
        description="도착 항구 이름 (시장 리드타임 기준 표의 값 그대로)",
        examples=["LOS ANGELES, CA"],
    )
    total_leadtime_p50: JsonNumberDecimal = Field(
        description="한국 출고 → 3PL 입고 총 리드타임 중앙값(일)", examples=[24.5]
    )
    total_leadtime_p90: JsonNumberDecimal = Field(
        description="총 리드타임 90분위(일). 계획 3PL 입고일을 정할 때 올림해 씀",
        examples=[31.2],
    )
    confidence_leadtime: str = Field(
        description="기준 리드타임 신뢰도 (market_leadtime_references.confidence_leadtime, 원천 값 그대로)",
        examples=["low"],
    )


class ShipmentScheduleObservationResponse(CamelModel):
    """일정 관측 하나 (shipment_schedule_snapshots)"""

    observed_at: datetime = Field(description="원천이 일정을 관측한 시각")
    stage: ShipmentStage = Field(description="그때의 운송 단계", examples=["AT_SEA"])
    expected_port_arrival_date: date | None = Field(
        description="그때의 예상 항구 도착일. 항공·미정이면 null",
        examples=["2026-07-01"],
    )
    expected_3pl_inbound_date: date = Field(
        alias=EXPECTED_3PL,
        description="그때의 예상 3PL 입고일",
        examples=["2026-07-06"],
    )


class ShipmentDetailResponse(ShipmentListItemResponse):
    """GET /api/v1/shipments/{shipmentId} 응답 (명세 4.3). 목록 행 필드 전부 + 아래 상세 필드"""

    expected_port_arrival_date: date | None = Field(
        description=(
            "현재 예상 항구 도착일 (shipments.current_expected_port_arrival_date). "
            "3PL 입고일과 다른 날짜다(BR-007). 항공 등으로 값이 없으면 null"
        ),
        examples=["2026-07-01"],
    )
    lines: list[ShipmentLineResponse] = Field(description="상세 줄 (줄 번호 순)")
    planned_basis: ShipmentPlannedBasisResponse = Field(
        description="계획 리드타임 근거"
    )
    schedule_history: list[ShipmentScheduleObservationResponse] = Field(
        description="일정 변경 이력. 관측 시각 오래된 순(같으면 관측 ID 순), 관측 전부"
    )
    data_basis_at: datetime | None = Field(
        description=(
            "데이터 기준 시각 = 이 선적 행을 마지막으로 바꾼 수집의 종료 시각 "
            "(data_sync_runs.finished_at, 분석 상세 dataBasisAt·수집 현황 lastSyncedAt 과 같은 열)"
        )
    )

    @classmethod
    def from_detail(cls, detail: ShipmentDetail) -> ShipmentDetailResponse:
        base = ShipmentListItemResponse.of(detail.item)
        return cls(
            **base.model_dump(),
            expected_port_arrival_date=detail.expected_port_arrival_date,
            lines=[
                ShipmentLineResponse(
                    line_no=line.line_no,
                    sku=SkuSummary(
                        sku_id=line.sku_id,
                        sku_code=line.sku_code,
                        product_name=line.product_name,
                    ),
                    shipped_quantity=line.shipped_quantity,
                )
                for line in detail.lines
            ],
            planned_basis=ShipmentPlannedBasisResponse(
                month=detail.planned_basis.month,
                us_port_name=detail.planned_basis.us_port_name,
                total_leadtime_p50=detail.planned_basis.total_leadtime_p50,
                total_leadtime_p90=detail.planned_basis.total_leadtime_p90,
                confidence_leadtime=detail.planned_basis.confidence_leadtime,
            ),
            schedule_history=[
                ShipmentScheduleObservationResponse(
                    observed_at=observation.observed_at,
                    stage=observation.stage,
                    expected_port_arrival_date=observation.expected_port_arrival_date,
                    expected_3pl_inbound_date=observation.expected_3pl_inbound_date,
                )
                for observation in detail.schedule_history
            ],
            data_basis_at=detail.data_basis_at,
        )
