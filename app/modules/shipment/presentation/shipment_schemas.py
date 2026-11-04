import uuid
from datetime import date

from pydantic import Field

from app.core.pagination import Page
from app.core.schemas import CamelModel
from app.core.summary_schemas import SkuSummary, WarehouseSummary
from app.modules.delay_analysis.presentation.analysis_run_schemas import (
    EXPECTED_3PL,
    PLANNED_3PL,
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
            "그 값이 계획 3PL 입고일보다 늦으면 true (서비스 플로우 1절의 감지·사후 평가 구분)"
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
