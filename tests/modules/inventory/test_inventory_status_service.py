import uuid
from datetime import date, timedelta

from app.core.pagination import PageParams
from app.core.security.tenant import TenantContext
from app.modules.inventory.application.inventory_status_rules import InventoryStatus
from app.modules.inventory.application.inventory_status_service import (
    InventoryStatusService,
)
from app.modules.inventory.infrastructure.repository.inventory_status_repository import (
    OperatingRule,
    StockRow,
)

"""
    거점별 재고 현황 서비스 단위 테스트 (DB 없음, 명세 4.4·D9·C22)
      - 기준일 D = 최신 재고 일자 + 1, 판매량 기간 [D − 28, D − 1], 운영 기준은 D 기준
      - 정렬: RISK → CAUTION → OK, stockoutDate 빠른 순(null 뒤), skuId, warehouseId. 정렬 뒤 페이지를 자른다
"""

TENANT = TenantContext(user_id=uuid.uuid7(), company_id=uuid.uuid7())
LATEST = date(2026, 6, 30)
RULE = OperatingRule(version_no=3, safety_stock_days=7, sales_basis_days=28)
WEST = uuid.UUID("00000000-0000-7000-8000-00000000000a")
EAST = uuid.UUID("00000000-0000-7000-8000-00000000000b")


def _row(sku: int, available: int, total_demand: int, warehouse=WEST) -> StockRow:
    return StockRow(
        warehouse_id=warehouse,
        warehouse_code="WH-WEST" if warehouse == WEST else "WH-EAST",
        region_code="WEST" if warehouse == WEST else "EAST",
        sku_id=uuid.UUID(f"00000000-0000-7000-8000-{sku:012d}"),
        sku_code=f"SKU-{sku:03d}",
        product_name=f"상품 {sku}",
        available_quantity=available,
        total_demand=total_demand,
    )


class FakeStatusRepository:
    def __init__(self, latest=LATEST, rule=RULE, rows=()):
        self.latest = latest
        self.rule = rule
        self.rows = list(rows)
        self.rule_calls: list = []
        self.stock_calls: list = []

    async def find_latest_inventory_date(self, company_id):
        return self.latest

    async def find_operating_rule(self, company_id, basis_date):
        self.rule_calls.append(basis_date)
        return self.rule

    async def find_stock_rows(self, company_id, **kwargs):
        self.stock_calls.append(kwargs)
        return self.rows


async def test_기준일은_최신_재고_일자_다음_날이고_판매량_기간은_직전_28일이다():
    repo = FakeStatusRepository(rows=[_row(1, 100, 280)])

    result = await InventoryStatusService(repo).get_page(TENANT, warehouse_id=WEST)

    assert repo.rule_calls == [date(2026, 7, 1)]
    assert repo.stock_calls == [
        {
            "inventory_date": LATEST,
            "demand_from": date(2026, 6, 3),
            "demand_to": date(2026, 6, 30),
            "warehouse_id": WEST,
        }
    ]
    assert (
        result.inventory_date,
        result.operating_rule_version_no,
        result.sales_basis_days,
        result.safety_stock_days,
        result.sales_basis_type,
    ) == (LATEST, 3, 28, 7, "ORDER_DEMAND")
    item = result.items[0]
    # 하루 10, 안전재고 70: 가용 100 → D+3 하회, D+9 품절 → CAUTION
    assert item.average_daily_sales == 10.0
    assert item.safety_stock_breach_date == date(2026, 7, 4)
    assert item.stockout_date == date(2026, 7, 10)
    assert item.status == InventoryStatus.CAUTION


async def test_위험_순_품절일_순_skuId_warehouseId_순으로_정렬한다():
    rows = [
        _row(1, 5000, 280),  # OK, 품절 D+499
        _row(2, 0, 0),  # OK, 판매 0 → 품절일 null (OK 중 맨 뒤)
        _row(3, 100, 280),  # CAUTION, 품절 D+9
        _row(4, 30, 280),  # RISK, 품절 D+2
        _row(5, 50, 280),  # RISK, 품절 D+4
        _row(4, 30, 280, warehouse=EAST),  # RISK, 품절 D+2, 같은 SKU 다른 창고
    ]
    repo = FakeStatusRepository(rows=rows)

    result = await InventoryStatusService(repo).get_page(TENANT)

    assert [(i.sku_code, i.warehouse_code, i.status) for i in result.items] == [
        ("SKU-004", "WH-WEST", InventoryStatus.RISK),
        ("SKU-004", "WH-EAST", InventoryStatus.RISK),
        ("SKU-005", "WH-WEST", InventoryStatus.RISK),
        ("SKU-003", "WH-WEST", InventoryStatus.CAUTION),
        ("SKU-001", "WH-WEST", InventoryStatus.OK),
        ("SKU-002", "WH-WEST", InventoryStatus.OK),
    ]
    assert result.items[-1].stockout_date is None
    assert result.items[0].stockout_date == date(2026, 7, 1) + timedelta(days=2)


async def test_정렬한_뒤_페이지를_자르고_전체_건수를_준다():
    rows = [_row(sku, 100 * sku, 280) for sku in range(1, 6)]
    repo = FakeStatusRepository(rows=rows)

    result = await InventoryStatusService(repo).get_page(
        TENANT, page=PageParams(page=2, size=2)
    )

    assert result.total_count == 5
    assert [i.sku_code for i in result.items] == ["SKU-003", "SKU-004"]


async def test_하루_판매량은_엔진처럼_소수_6자리로_반올림한다():
    repo = FakeStatusRepository(rows=[_row(1, 100, 519)])  # 519 / 28 = 18.535714285…

    result = await InventoryStatusService(repo).get_page(TENANT)

    assert result.items[0].average_daily_sales == 18.535714


async def test_적재된_재고가_없으면_빈_결과이고_운영_기준을_읽지_않는다():
    repo = FakeStatusRepository(latest=None)

    result = await InventoryStatusService(repo).get_page(TENANT)

    assert (result.items, result.total_count, result.inventory_date) == ([], 0, None)
    assert repo.rule_calls == [] and repo.stock_calls == []


async def test_적용할_운영_기준이_없으면_빈_결과다():
    repo = FakeStatusRepository(rule=None)

    result = await InventoryStatusService(repo).get_page(TENANT)

    assert (result.items, result.operating_rule_version_no) == ([], None)
    assert repo.stock_calls == []
