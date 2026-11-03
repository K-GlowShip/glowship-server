import uuid
from datetime import date, datetime

from pydantic import Field

from app.core.pagination import Page
from app.core.schemas import CamelModel
from app.core.summary_schemas import WarehouseSummary
from app.modules.order.application.order_list_service import (
    OrderListItem,
    OrderListResult,
)
from app.modules.order.infrastructure.order_enums import (
    CustomerRegion,
    OrderStatus,
    SalesChannel,
)

MAX_QUERY_LENGTH = 100


class OrderListItemResponse(CamelModel):
    """주문 탭의 한 행 (명세 4.3 GET /orders).
    출고 관련 날짜 4개(D8)는 서비스가 계산한다. actualDispatchDate 는 수집 변경 기록으로 역산한 값이고,
    securedDate·plannedDispatchDate·expectedDeliveryDate 는 아직 계산하지 않아 null 이다.
    주문 상세 lines[] 는 보류했다(2026-11-03)."""

    order_id: uuid.UUID
    order_code: str = Field(
        description="화면의 주문 번호 (orders.order_code)",
        examples=["A-ORD-20260630-0177"],
    )
    sales_channel: SalesChannel = Field(examples=["AMAZON"])
    customer_region: CustomerRegion = Field(
        description="WEST(서부)·EAST(동부)", examples=["EAST"]
    )
    warehouse: WarehouseSummary = Field(description="출고 창고")
    ordered_at: datetime = Field(
        description="주문 일시 (UTC). 목록 정렬 기준(최신순)",
        examples=["2026-06-30T23:31:48Z"],
    )
    promised_delivery_at: datetime = Field(
        description="약속 배송 완료 시각 (UTC)", examples=["2026-07-03T23:59:59Z"]
    )
    status: OrderStatus = Field(
        description=(
            "OPEN(미출고)·PARTIALLY_SHIPPED(부분 출고)·SHIPPED(출고 완료)·DELIVERED(배송 완료)·"
            "CANCELLED(취소)·REMOVED_AT_SOURCE(원천 삭제). "
            "OPEN 에는 재고 미확보 주문과 확보 후 출고 대기 주문이 함께 있다"
        ),
        examples=["OPEN"],
    )
    secured_date: date | None = Field(
        description=(
            "재고 확보일 (UTC 날짜). 명세 4.3 은 OPEN 주문에서 값이 있으면 확보 후 출고 대기, null 이면 미확보로 구분하지만 "
            "아직 계산하지 않아 항상 null 이다. 화면은 이 값으로 확보 여부를 구분하지 말 것"
        ),
        examples=[None],
    )
    planned_dispatch_date: date | None = Field(
        description="출고 예정일 (UTC 날짜). 아직 계산하지 않아 항상 null",
        examples=[None],
    )
    actual_dispatch_date: date | None = Field(
        description=(
            "실제 출고일 (UTC 날짜). 주문 상품 줄의 첫 변경(ORDER_LINE UPDATE)이 수집된 날짜 − 1일로 역산. "
            "출고 전이거나 변경 기록이 없으면 null"
        ),
        examples=["2026-06-28"],
    )
    expected_delivery_date: date | None = Field(
        description="배송 예정일 (UTC 날짜). 아직 계산하지 않아 항상 null",
        examples=[None],
    )

    @classmethod
    def of(cls, item: OrderListItem) -> OrderListItemResponse:
        return cls(
            order_id=item.order_id,
            order_code=item.order_code,
            sales_channel=item.sales_channel,
            customer_region=item.customer_region,
            warehouse=WarehouseSummary(
                warehouse_id=item.warehouse_id,
                warehouse_code=item.warehouse_code,
                region_code=item.region_code,
            ),
            ordered_at=item.ordered_at,
            promised_delivery_at=item.promised_delivery_at,
            status=item.status,
            secured_date=item.secured_date,
            planned_dispatch_date=item.planned_dispatch_date,
            actual_dispatch_date=item.actual_dispatch_date,
            expected_delivery_date=item.expected_delivery_date,
        )


class OrderListResponse(Page[OrderListItemResponse]):
    """GET /api/v1/orders 응답. 공통 목록 모양(items·page·size·totalCount·totalPages)"""

    @classmethod
    def from_result(cls, result: OrderListResult) -> OrderListResponse:
        return cls.of(
            [OrderListItemResponse.of(item) for item in result.items],
            result.total_count,
            result.page,
        )
