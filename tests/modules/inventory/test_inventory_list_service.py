import uuid
from dataclasses import dataclass, field
from datetime import date

import pytest

from app.core.pagination import PageParams
from app.core.security.tenant import TenantContext
from app.modules.inventory.application.inventory_list_service import (
    InventoryListService,
)
from app.modules.inventory.infrastructure.repository.inventory_repository import (
    InventoryFilter,
    InventoryPage,
    InventoryRow,
)

TENANT = TenantContext(user_id=uuid.uuid7(), company_id=uuid.uuid7())
LATEST = date(2026, 6, 30)


@dataclass
class FakeInventoryRepository:
    latest_date: date | None = LATEST
    page: InventoryPage = field(
        default_factory=lambda: InventoryPage(rows=[], total_count=0)
    )
    calls: list = field(default_factory=list)

    async def find_latest_date(self, company_id):
        self.calls.append(("find_latest_date", company_id))
        return self.latest_date

    async def find_page(self, company_id, filters, **kwargs):
        self.calls.append(("find_page", company_id, filters, kwargs))
        return self.page


def _row(**overrides) -> InventoryRow:
    values = {
        "inventory_date": LATEST,
        "warehouse_id": uuid.uuid7(),
        "warehouse_code": "WH-WEST",
        "region_code": "WEST",
        "sku_id": uuid.uuid7(),
        "sku_code": "SKU-001",
        "product_name": "A Glow Hydra Toner Pad 60ea",
        "on_hand_quantity": 1240,
        "reserved_quantity": 310,
        "available_quantity": 930,
        "outbound_quantity": 85,
        "data_sync_run_id": uuid.uuid7(),
    }
    values.update(overrides)
    return InventoryRow(**values)


def _service(rows=(), total_count: int = 0, latest_date: date | None = LATEST):
    repo = FakeInventoryRepository(
        latest_date=latest_date,
        page=InventoryPage(rows=list(rows), total_count=total_count),
    )
    return InventoryListService(repo=repo), repo


async def test_일자를_생략하면_최신_적재_일자로_조회하고_응답에_그_일자를_넣는다():
    service, repo = _service()

    result = await service.get_page(TENANT)

    [(_, company_id), (_, _, filters, kwargs)] = repo.calls
    assert company_id == TENANT.company_id
    assert filters == InventoryFilter(inventory_date=LATEST)
    assert kwargs == {"offset": 0, "limit": 20}
    assert result.inventory_date == LATEST
    assert result.items == [] and result.total_count == 0
    assert (result.page.page, result.page.size) == (1, 20)


async def test_적재된_재고가_없으면_목록을_조회하지_않고_빈_결과와_None_을_준다():
    service, repo = _service(latest_date=None)

    result = await service.get_page(TENANT, page=PageParams(page=3, size=50))

    assert [call[0] for call in repo.calls] == ["find_latest_date"]
    assert result.items == [] and result.total_count == 0
    assert result.inventory_date is None
    assert (result.page.page, result.page.size) == (3, 50)


async def test_일자를_주면_최신_일자를_조회하지_않고_필터를_그대로_넘긴다():
    service, repo = _service()
    warehouse_id, sku_id = uuid.UUID(int=1), uuid.UUID(int=2)

    result = await service.get_page(
        TENANT,
        inventory_date=date(2025, 1, 1),
        warehouse_id=warehouse_id,
        sku_id=sku_id,
        query="  toner  ",
        page=PageParams(page=2, size=50),
    )

    [(name, _, filters, kwargs)] = repo.calls
    assert name == "find_page"
    assert filters == InventoryFilter(
        inventory_date=date(2025, 1, 1),
        warehouse_id=warehouse_id,
        sku_id=sku_id,
        query="toner",
    )
    assert kwargs == {"offset": 50, "limit": 50}
    assert result.inventory_date == date(2025, 1, 1)


@pytest.mark.parametrize("query", [None, "", "   "])
async def test_검색어가_비어_있으면_없는_것으로_본다(query):
    service, repo = _service()

    await service.get_page(TENANT, query=query)

    assert repo.calls[1][2].query is None


async def test_행을_InventoryListItem_으로_바꾸고_창고_SKU_수량을_그대로_옮긴다():
    rows = [
        _row(),
        _row(
            sku_code="SKU-002",
            on_hand_quantity=0,
            reserved_quantity=0,
            available_quantity=0,
        ),
    ]
    service, _ = _service(rows=rows, total_count=66)

    result = await service.get_page(TENANT)

    [item_a, item_b] = result.items
    assert (item_a.warehouse_id, item_a.warehouse_code, item_a.region_code) == (
        rows[0].warehouse_id,
        "WH-WEST",
        "WEST",
    )
    assert (item_a.sku_id, item_a.sku_code, item_a.product_name) == (
        rows[0].sku_id,
        "SKU-001",
        "A Glow Hydra Toner Pad 60ea",
    )
    assert (
        item_a.on_hand_quantity,
        item_a.reserved_quantity,
        item_a.available_quantity,
        item_a.outbound_quantity,
    ) == (1240, 310, 930, 85)
    assert item_a.data_sync_run_id == rows[0].data_sync_run_id
    assert item_b.sku_code == "SKU-002" and item_b.on_hand_quantity == 0
    assert result.total_count == 66
