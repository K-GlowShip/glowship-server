import datetime as dt

from pydantic import Field

from app.core.pagination import Page
from app.core.schemas import CamelModel
from app.core.summary_schemas import SkuSummary, WarehouseSummary
from app.modules.delay_analysis.infrastructure.analysis_enums import SalesBasisType
from app.modules.inventory.application.inventory_status_rules import InventoryStatus
from app.modules.inventory.application.inventory_status_service import (
    InventoryStatusItem,
    InventoryStatusResult,
)


class InventoryStatusItemResponse(CamelModel):
    """거점별 재고 현황 한 행 (명세 4.4 GET /inventory-status). 최신 재고와 운영 기준으로 계산한 참고값."""

    warehouse: WarehouseSummary = Field(description="창고 요약")
    sku: SkuSummary = Field(description="상품 요약")
    available_quantity: int = Field(
        description="가용 수량 (inventoryDate 재고)", examples=[930]
    )
    average_daily_sales: float = Field(
        description=(
            "하루 판매량: 최근 salesBasisDays 일의 유효 주문 수요 평균(ORDER_DEMAND, D9). 소수 6자리(분석 결과의 averageDailySales 와 같은 자릿수)"
        ),
        examples=[12.5],
    )
    safety_stock_breach_date: dt.date | None = Field(
        description=(
            "안전재고 하회일: 기준일 D(= inventoryDate + 1)부터 하루 판매량씩 빼서 남은 수량이 "
            "안전재고(ceil(하루 판매량 × safetyStockDays))보다 처음 작아지는 날. 입고 예정 미반영. 판매량 0 이면 null"
        ),
        examples=["2026-07-04"],
    )
    stockout_date: dt.date | None = Field(
        description="품절 예상일: 남은 수량이 0 이하가 되는 첫날. 입고 예정 미반영. 판매량 0 이면 null",
        examples=["2026-07-10"],
    )
    status: InventoryStatus = Field(
        description=(
            "RISK(하회일 ≤ D) · CAUTION(D < 하회일 ≤ D + safetyStockDays) · OK(그 밖)"
        ),
        examples=["CAUTION"],
    )

    @classmethod
    def of(cls, item: InventoryStatusItem) -> InventoryStatusItemResponse:
        return cls(
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
            available_quantity=item.available_quantity,
            average_daily_sales=item.average_daily_sales,
            safety_stock_breach_date=item.safety_stock_breach_date,
            stockout_date=item.stockout_date,
            status=item.status,
        )


class InventoryStatusResponse(Page[InventoryStatusItemResponse]):
    """GET /api/v1/inventory-status 응답. 공통 목록 모양에 계산 기준을 형제 필드로 더한다 (응답 규칙 6.2)."""

    inventory_date: dt.date | None = Field(
        description="계산에 쓴 재고 일자 (= 고객사 최신 적재 일자, 기준일 D − 1). 적재된 재고가 없으면 null",
        examples=["2026-06-30"],
    )
    operating_rule_version_no: int | None = Field(
        description="적용 운영 기준 버전 (effective_from ≤ D 중 최대). 없으면 null",
        examples=[1],
    )
    sales_basis_days: int | None = Field(
        description='하루 판매량 기간(일). 화면의 "최근 N일 평균" (7절 U2)',
        examples=[28],
    )
    safety_stock_days: int | None = Field(description="안전재고 일수", examples=[14])
    sales_basis_type: SalesBasisType = Field(
        description="하루 판매량 기준: ORDER_DEMAND(유효 주문 수요, D9)",
        examples=["ORDER_DEMAND"],
    )

    @classmethod
    def from_result(cls, result: InventoryStatusResult) -> InventoryStatusResponse:
        return cls.of(
            [InventoryStatusItemResponse.of(item) for item in result.items],
            result.total_count,
            result.page,
            inventory_date=result.inventory_date,
            operating_rule_version_no=result.operating_rule_version_no,
            sales_basis_days=result.sales_basis_days,
            safety_stock_days=result.safety_stock_days,
            sales_basis_type=result.sales_basis_type,
        )
