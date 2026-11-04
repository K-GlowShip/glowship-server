import uuid
from dataclasses import dataclass
from datetime import date, timedelta

from app.core.pagination import PageParams
from app.core.security.tenant import TenantContext
from app.modules.delay_analysis.infrastructure.analysis_enums import SalesBasisType
from app.modules.inventory.application.inventory_status_rules import (
    InventoryStatus,
    average_daily_sales,
    judge_inventory,
)
from app.modules.inventory.infrastructure.repository.inventory_status_repository import (
    InventoryStatusRepository,
    StockRow,
)

"""
    거점별 재고 현황 서비스 (API 명세 v6.2 4.4 GET /inventory-status, D9·C22, 홈 대시보드 SCR-002)
      - 조회할 때 계산하고 저장하지 않는 참고값이다. 입고 예정(지연 입고 포함)은 반영하지 않는다.
        지연 입고를 반영한 값은 분석 결과(GET /analysis-runs/{id})에만 있다.
      - 기준일 D = 최신 재고 일자 + 1일, 판매량 기간 = [D − sales_basis_days, D − 1] (응답 salesBasisDays),
        하루 판매량 기준은 유효 주문 수요(ORDER_DEMAND, D9 제안).
      - 정렬: status 위험 순(RISK → CAUTION → OK), 같으면 stockoutDate 빠른 순(null 은 뒤), 같으면 skuId,
        같은 SKU 가 여러 창고에 있으면 warehouseId 순. 계산한 상태로 정렬해야 하므로 모든 행을 계산한 뒤 페이지를 자른다
        (고객사 한 곳의 행은 창고 수 × SKU 수라 많지 않다).
      - 적재된 재고나 적용할 운영 기준이 없으면 빈 결과다.
"""

# 하루 판매량은 엔진이 inventory_impact_results 에 저장하는 값과 같게 소수 6자리 (엔진 storage: round(mu, 6))
SALES_DECIMALS = 6

_STATUS_ORDER = {
    InventoryStatus.RISK: 0,
    InventoryStatus.CAUTION: 1,
    InventoryStatus.OK: 2,
}


@dataclass(frozen=True)
class InventoryStatusItem:
    warehouse_id: uuid.UUID
    warehouse_code: str
    region_code: str
    sku_id: uuid.UUID
    sku_code: str
    product_name: str
    available_quantity: int
    average_daily_sales: float
    safety_stock_breach_date: date | None
    stockout_date: date | None
    status: InventoryStatus


@dataclass(frozen=True)
class InventoryStatusResult:
    items: list[InventoryStatusItem]
    total_count: int
    page: PageParams
    inventory_date: date | None
    operating_rule_version_no: int | None
    sales_basis_days: int | None
    safety_stock_days: int | None
    sales_basis_type: SalesBasisType


def _sort_key(item: InventoryStatusItem):
    return (
        _STATUS_ORDER[item.status],
        item.stockout_date is None,
        item.stockout_date or date.max,
        item.sku_id,
        item.warehouse_id,
    )


class InventoryStatusService:
    def __init__(self, repo: InventoryStatusRepository):
        self.repo = repo

    async def get_page(
        self,
        tenant: TenantContext,
        *,
        warehouse_id: uuid.UUID | None = None,
        page: PageParams | None = None,
    ) -> InventoryStatusResult:
        page = page or PageParams()
        company_id = tenant.company_id
        empty = InventoryStatusResult(
            items=[],
            total_count=0,
            page=page,
            inventory_date=None,
            operating_rule_version_no=None,
            sales_basis_days=None,
            safety_stock_days=None,
            sales_basis_type=SalesBasisType.ORDER_DEMAND,
        )
        inventory_date = await self.repo.find_latest_inventory_date(company_id)
        if inventory_date is None:
            return empty
        basis_date = inventory_date + timedelta(days=1)
        rule = await self.repo.find_operating_rule(company_id, basis_date)
        if rule is None:
            return empty
        rows = await self.repo.find_stock_rows(
            company_id,
            inventory_date=inventory_date,
            demand_from=basis_date - timedelta(days=rule.sales_basis_days),
            demand_to=basis_date - timedelta(days=1),
            warehouse_id=warehouse_id,
        )
        items = sorted(
            (
                self._build_item(
                    row, rule.sales_basis_days, rule.safety_stock_days, basis_date
                )
                for row in rows
            ),
            key=_sort_key,
        )
        return InventoryStatusResult(
            items=items[page.offset : page.offset + page.limit],
            total_count=len(items),
            page=page,
            inventory_date=inventory_date,
            operating_rule_version_no=rule.version_no,
            sales_basis_days=rule.sales_basis_days,
            safety_stock_days=rule.safety_stock_days,
            sales_basis_type=SalesBasisType.ORDER_DEMAND,
        )

    @staticmethod
    def _build_item(
        row: StockRow, sales_basis_days: int, safety_stock_days: int, basis_date: date
    ) -> InventoryStatusItem:
        daily_sales = average_daily_sales(row.total_demand, sales_basis_days)
        outlook = judge_inventory(
            available_quantity=row.available_quantity,
            daily_sales=daily_sales,
            safety_stock_days=safety_stock_days,
            basis_date=basis_date,
        )
        return InventoryStatusItem(
            warehouse_id=row.warehouse_id,
            warehouse_code=row.warehouse_code,
            region_code=row.region_code,
            sku_id=row.sku_id,
            sku_code=row.sku_code,
            product_name=row.product_name,
            available_quantity=row.available_quantity,
            average_daily_sales=round(daily_sales, SALES_DECIMALS),
            safety_stock_breach_date=outlook.safety_stock_breach_date,
            stockout_date=outlook.stockout_date,
            status=outlook.status,
        )
