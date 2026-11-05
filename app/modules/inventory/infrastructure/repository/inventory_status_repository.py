import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import UUID, Date, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

"""
    거점별 재고 현황 입력 조회 (API 명세 v6.2 4.4 GET /inventory-status, D9·C22, 홈 대시보드 SCR-002)
      - 계산은 하지 않고 판정 규칙(inventory_status_rules)에 넣을 값만 읽는다.
      - 기준일 D = 고객사의 최신 재고 일자 + 1일. 재고 행은 D − 1 일자(= 최신 재고 일자)의 창고·SKU별 가용 수량이다.
      - 운영 기준 = effective_from ≤ D 인 것 중 version_no 최대 (엔진 snapshot.build 와 같음).
      - 하루 판매량의 재료 = D 직전 sales_basis_days 일(D − sales_basis_days ~ D − 1)의 유효 주문 수요 합.
        유효 주문 수요 = CANCELLED·REMOVED_AT_SOURCE 가 아닌 주문의 ACTIVE 상세 ordered_quantity,
        날짜는 ordered_at 의 UTC 날짜, 창고는 주문의 출고 창고(orders.warehouse_id) (엔진 store.demand 와 같음).
      - 운영에서는 RLS 가 고객사 범위를 강제하지만, 로컬 개발 DB 는 슈퍼유저라 RLS 를 우회하므로
        조건을 명시해 같은 결과가 나오게 한다 (ERD 설명 4.6).
"""

_UUID = UUID(as_uuid=True)

_LATEST_INVENTORY_DATE = text(
    "SELECT max(inventory_date) FROM inventory_daily WHERE company_id = :company_id"
).bindparams(bindparam("company_id", type_=_UUID))

_OPERATING_RULE = text(
    """
    SELECT version_no, safety_stock_days, sales_basis_days
      FROM operating_rule_sets
     WHERE company_id = :company_id AND effective_from <= :basis_date
     ORDER BY version_no DESC
     LIMIT 1
    """
).bindparams(
    bindparam("company_id", type_=_UUID), bindparam("basis_date", type_=Date())
)

_STOCK_ROWS = text(
    """
    WITH demand AS (
        SELECT o.warehouse_id, l.sku_id, SUM(l.ordered_quantity) AS total_demand
          FROM orders o
          JOIN order_lines l ON l.company_id = o.company_id AND l.order_id = o.order_id
         WHERE o.company_id = :company_id
           AND o.status NOT IN ('CANCELLED', 'REMOVED_AT_SOURCE')
           AND l.line_status = 'ACTIVE'
           AND (o.ordered_at AT TIME ZONE 'UTC')::date >= :demand_from
           AND (o.ordered_at AT TIME ZONE 'UTC')::date <= :demand_to
         GROUP BY o.warehouse_id, l.sku_id
    )
    SELECT w.warehouse_id, w.warehouse_code, w.region_code,
           k.sku_id, k.sku_code, k.product_name,
           i.available_quantity,
           COALESCE(d.total_demand, 0) AS total_demand
      FROM inventory_daily i
      JOIN warehouses w ON w.company_id = i.company_id AND w.warehouse_id = i.warehouse_id
      JOIN skus k ON k.company_id = i.company_id AND k.sku_id = i.sku_id
      LEFT JOIN demand d ON d.warehouse_id = i.warehouse_id AND d.sku_id = i.sku_id
     WHERE i.company_id = :company_id
       AND i.inventory_date = :inventory_date
       AND (CAST(:warehouse_id AS uuid) IS NULL OR i.warehouse_id = :warehouse_id)
    """
).bindparams(
    bindparam("company_id", type_=_UUID),
    bindparam("inventory_date", type_=Date()),
    bindparam("warehouse_id", type_=_UUID),
    bindparam("demand_from", type_=Date()),
    bindparam("demand_to", type_=Date()),
)


@dataclass(frozen=True)
class OperatingRule:
    version_no: int
    safety_stock_days: int
    sales_basis_days: int


@dataclass(frozen=True)
class StockRow:
    """최신 재고 일자의 창고·SKU 한 건과 판매량 기간의 유효 주문 수요 합"""

    warehouse_id: uuid.UUID
    warehouse_code: str
    region_code: str
    sku_id: uuid.UUID
    sku_code: str
    product_name: str
    available_quantity: int
    total_demand: int


class InventoryStatusRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # Read
    async def find_latest_inventory_date(self, company_id: uuid.UUID) -> date | None:
        """고객사의 최신 재고 일자 (= 기준일 D − 1). 적재된 재고가 없으면 None."""
        return await self.db.scalar(_LATEST_INVENTORY_DATE, {"company_id": company_id})

    async def find_operating_rule(
        self, company_id: uuid.UUID, basis_date: date
    ) -> OperatingRule | None:
        """기준일에 적용되는 운영 기준: effective_from ≤ D 중 version_no 최대. 없으면 None."""
        row = (
            await self.db.execute(
                _OPERATING_RULE, {"company_id": company_id, "basis_date": basis_date}
            )
        ).one_or_none()
        if row is None:
            return None
        return OperatingRule(
            version_no=int(row.version_no),
            safety_stock_days=int(row.safety_stock_days),
            sales_basis_days=int(row.sales_basis_days),
        )

    async def find_stock_rows(
        self,
        company_id: uuid.UUID,
        *,
        inventory_date: date,
        demand_from: date,
        demand_to: date,
        warehouse_id: uuid.UUID | None = None,
    ) -> list[StockRow]:
        """inventory_date 의 창고·SKU별 가용 수량과 [demand_from, demand_to] 의 유효 주문 수요 합 (정렬 없음)."""
        rows = await self.db.execute(
            _STOCK_ROWS,
            {
                "company_id": company_id,
                "inventory_date": inventory_date,
                "warehouse_id": warehouse_id,
                "demand_from": demand_from,
                "demand_to": demand_to,
            },
        )
        return [
            StockRow(
                warehouse_id=row.warehouse_id,
                warehouse_code=row.warehouse_code,
                region_code=row.region_code,
                sku_id=row.sku_id,
                sku_code=row.sku_code,
                product_name=row.product_name,
                available_quantity=int(row.available_quantity),
                total_demand=int(row.total_demand),
            )
            for row in rows
        ]
