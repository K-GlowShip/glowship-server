import uuid
from datetime import datetime

from pydantic import Field

from app.core.pagination import Page
from app.core.schemas import CamelModel
from app.core.summary_schemas import SkuSummary, WarehouseSummary
from app.modules.order.application.order_list_service import (
    OrderLineItem,
    OrderListItem,
    OrderListResult,
)
from app.modules.order.infrastructure.order_enums import (
    CustomerRegion,
    OrderLineStatus,
    OrderStatus,
    SalesChannel,
)

MAX_QUERY_LENGTH = 100


class OrderLineResponse(CamelModel):
    """주문 상세 한 줄 (명세 4.3 lines[]). lineNo 오름차순, 안쪽 배열이라 페이지 없이 그대로 (응답 규칙 6.2)."""

    line_no: int = Field(examples=[1])
    sku: SkuSummary
    ordered_quantity: int = Field(examples=[3])
    shipped_quantity: int = Field(
        description="출고된 수량. 0 ~ orderedQuantity", examples=[3]
    )
    line_status: OrderLineStatus = Field(
        description="ACTIVE(정상)·REMOVED_AT_SOURCE(원천 삭제)", examples=["ACTIVE"]
    )

    @classmethod
    def of(cls, line: OrderLineItem) -> OrderLineResponse:
        return cls(
            line_no=line.line_no,
            sku=SkuSummary(
                sku_id=line.sku_id,
                sku_code=line.sku_code,
                product_name=line.product_name,
            ),
            ordered_quantity=line.ordered_quantity,
            shipped_quantity=line.shipped_quantity,
            line_status=line.line_status,
        )


class OrderListItemResponse(CamelModel):
    """주문 탭의 한 행 (명세 4.3 GET /orders).
    D8 출고 파생값(securedDate·plannedDispatchDate·actualDispatchDate·expectedDeliveryDate)은 아직 정해지지 않아 없다."""

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
    lines: list[OrderLineResponse] = Field(description="주문 상세. lineNo 순")

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
            lines=[OrderLineResponse.of(line) for line in item.lines],
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
