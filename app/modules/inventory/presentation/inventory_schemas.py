import uuid
from datetime import date

from pydantic import Field

from app.core.pagination import Page
from app.core.schemas import CamelModel
from app.core.summary_schemas import SkuSummary, WarehouseSummary
from app.modules.inventory.application.inventory_list_service import (
    InventoryListItem,
    InventoryListResult,
)

MAX_QUERY_LENGTH = 100


class InventoryListItemResponse(CamelModel):
    """재고 탭의 한 행 (명세 4.3 GET /inventory). 한 일자의 창고·SKU 조합 하나."""

    inventory_date: date = Field(
        description="재고 기준 일자 (UTC 날짜)", examples=["2026-06-30"]
    )
    warehouse: WarehouseSummary = Field(description="창고 요약")
    sku: SkuSummary = Field(description="상품 요약")
    on_hand_quantity: int = Field(
        description="보유 수량. 항상 reservedQuantity + availableQuantity (ERD ck5)",
        examples=[1240],
    )
    reserved_quantity: int = Field(
        description=(
            "예약 수량. 다음 날 출고 대기분이 이미 들어 있으므로 "
            "미출고 주문 수량을 다시 차감하지 않는다 (BR-003)"
        ),
        examples=[310],
    )
    available_quantity: int = Field(description="가용 수량", examples=[930])
    outbound_quantity: int = Field(description="당일 출고 수량", examples=[85])
    data_sync_run_id: uuid.UUID = Field(description="이 행을 적재한 수집 실행 ID")

    @classmethod
    def of(cls, item: InventoryListItem) -> InventoryListItemResponse:
        return cls(
            inventory_date=item.inventory_date,
            warehouse=WarehouseSummary(
                warehouse_id=item.warehouse_id,
                warehouse_code=item.warehouse_code,
                region_code=item.region_code,
            ),
            sku=SkuSummary(
                sku_id=item.sku_id,
                sku_code=item.sku_code,
                product_name=item.product_name,
            ),
            on_hand_quantity=item.on_hand_quantity,
            reserved_quantity=item.reserved_quantity,
            available_quantity=item.available_quantity,
            outbound_quantity=item.outbound_quantity,
            data_sync_run_id=item.data_sync_run_id,
        )


class InventoryListResponse(Page[InventoryListItemResponse]):
    """GET /api/v1/inventory 응답. 공통 목록 모양(items·page·size·totalCount·totalPages)에
    이번 응답의 기준 일자(inventoryDate)를 형제 필드로 더한다 (응답 규칙 6.2)."""

    inventory_date: date | None = Field(
        description=(
            "이번 응답이 보여주는 재고 기준 일자. date 를 생략하면 고객사의 최신 적재 일자. "
            "적재된 재고가 없으면 null (items 도 [])"
        ),
        examples=["2026-06-30"],
    )

    @classmethod
    def from_result(cls, result: InventoryListResult) -> InventoryListResponse:
        return cls.of(
            [InventoryListItemResponse.of(item) for item in result.items],
            result.total_count,
            result.page,
            inventory_date=result.inventory_date,
        )
