import uuid
from dataclasses import dataclass, field
from datetime import date

import pytest

from app.core.pagination import PageParams
from app.core.security.tenant import TenantContext
from app.modules.shipment.application.shipment_list_service import (
    ShipmentListService,
)
from app.modules.shipment.infrastructure.repository.shipment_repository import (
    ShipmentFilter,
    ShipmentLineRow,
    ShipmentListPage,
    ShipmentListRow,
)

TENANT = TenantContext(user_id=uuid.uuid7(), company_id=uuid.uuid7())
SHIPMENT_ID = uuid.uuid7()
PLANNED = date(2026, 7, 12)


@dataclass
class FakeShipmentRepository:
    page: ShipmentListPage = field(
        default_factory=lambda: ShipmentListPage(rows=[], total_count=0)
    )
    lines: list[ShipmentLineRow] = field(default_factory=list)
    calls: list = field(default_factory=list)

    async def find_page(self, company_id, filters, **kwargs):
        self.calls.append(("find_page", company_id, filters, kwargs))
        return self.page

    async def find_lines(self, company_id, shipment_ids):
        self.calls.append(("find_lines", company_id, list(shipment_ids)))
        return [line for line in self.lines if line.shipment_id in shipment_ids]


def _row(**overrides) -> ShipmentListRow:
    values = {
        "shipment_id": SHIPMENT_ID,
        "shipment_code": "OCN-20260601",
        "transport_mode": "OCEAN",
        "stage": "AT_SEA",
        "planned_kr_release_date": date(2026, 6, 17),
        "planned_3pl_inbound_date": PLANNED,
        "expected_3pl_inbound_date": date(2026, 7, 6),
        "actual_3pl_inbound_date": None,
        "warehouse_id": uuid.uuid7(),
        "warehouse_code": "WH-WEST",
        "region_code": "WEST",
        "us_port_name": "LOS ANGELES, CA",
        "origin_region_code": "KR",
    }
    values.update(overrides)
    return ShipmentListRow(**values)


def _line(shipment_id=SHIPMENT_ID, **overrides) -> ShipmentLineRow:
    values = {
        "shipment_id": shipment_id,
        "line_no": 1,
        "sku_id": uuid.uuid7(),
        "shipped_quantity": 100,
        "sku_code": "SKU-001",
        "product_name": "A Glow Hydra Toner Pad 60ea",
    }
    values.update(overrides)
    return ShipmentLineRow(**values)


def _service(rows=(), lines=(), total_count: int = 0):
    repo = FakeShipmentRepository(
        page=ShipmentListPage(rows=list(rows), total_count=total_count),
        lines=list(lines),
    )
    return ShipmentListService(repo=repo), repo


async def test_필터와_페이지를_리포지토리에_넘기고_페이지의_선적_ID_로_상세를_읽는다():
    warehouse_id = uuid.UUID(int=1)
    service, repo = _service(rows=[_row()], total_count=83)

    result = await service.get_page(
        TENANT,
        stage="AT_SEA",
        transport_mode="OCEAN",
        warehouse_id=warehouse_id,
        query="  ocn  ",
        page=PageParams(page=2, size=50),
    )

    [(_, company_id, filters, kwargs), (_, _, shipment_ids)] = repo.calls
    assert company_id == TENANT.company_id
    assert filters == ShipmentFilter(
        stage="AT_SEA",
        transport_mode="OCEAN",
        warehouse_id=warehouse_id,
        query="ocn",
    )
    assert kwargs == {"offset": 50, "limit": 50}
    assert shipment_ids == [SHIPMENT_ID]
    assert result.total_count == 83
    assert (result.page.page, result.page.size) == (2, 50)


async def test_페이지를_생략하면_첫_페이지_기본_크기다():
    service, repo = _service()

    result = await service.get_page(TENANT)

    assert repo.calls[0][3] == {"offset": 0, "limit": 20}
    assert repo.calls[0][2] == ShipmentFilter()
    assert result.items == [] and result.total_count == 0
    assert (result.page.page, result.page.size) == (1, 20)


@pytest.mark.parametrize("query", [None, "", "   "])
async def test_검색어가_비어_있으면_없는_것으로_본다(query):
    service, repo = _service()

    await service.get_page(TENANT, query=query)

    assert repo.calls[0][2].query is None


async def test_행을_ShipmentListItem_으로_바꾸고_경로와_창고_날짜를_그대로_옮긴다():
    row = _row()
    service, _ = _service(rows=[row], lines=[_line()], total_count=1)

    [item] = (await service.get_page(TENANT)).items

    assert (item.shipment_id, item.shipment_code, item.transport_mode) == (
        SHIPMENT_ID,
        "OCN-20260601",
        "OCEAN",
    )
    assert item.route == "KR → LOS ANGELES"
    assert (item.warehouse_id, item.warehouse_code, item.region_code) == (
        row.warehouse_id,
        "WH-WEST",
        "WEST",
    )
    assert item.stage == "AT_SEA"
    assert (
        item.planned_kr_release_date,
        item.planned_3pl_inbound_date,
        item.expected_3pl_inbound_date,
        item.actual_3pl_inbound_date,
    ) == (date(2026, 6, 17), PLANNED, date(2026, 7, 6), None)


async def test_한국_출고_창고가_없으면_경로_출발지는_KR_이다():
    service, _ = _service(rows=[_row(origin_region_code=None)], total_count=1)

    [item] = (await service.get_page(TENANT)).items

    assert item.route == "KR → LOS ANGELES"


async def test_대표_상품은_수량이_가장_많은_줄이고_같으면_줄_번호가_작은_줄이며_품목_수는_서로_다른_SKU_수다():
    top = _line(line_no=2, shipped_quantity=300, sku_code="SKU-002")
    tie = _line(line_no=3, shipped_quantity=300, sku_code="SKU-003")
    small = _line(line_no=1, shipped_quantity=10, sku_code="SKU-001")
    service, _ = _service(rows=[_row()], lines=[small, top, tie], total_count=1)

    [item] = (await service.get_page(TENANT)).items

    assert (
        item.representative_sku_id,
        item.representative_sku_code,
        item.representative_product_name,
    ) == (top.sku_id, "SKU-002", top.product_name)
    assert item.sku_count == 3


async def test_상세_줄이_없으면_대표_상품은_None_이고_품목_수는_0이다():
    service, _ = _service(rows=[_row()], total_count=1)

    [item] = (await service.get_page(TENANT)).items

    assert item.representative_sku_id is None
    assert item.representative_sku_code is None
    assert item.representative_product_name is None
    assert item.sku_count == 0


async def test_상세_줄을_선적별로_나눠_붙인다():
    other_id = uuid.uuid7()
    lines = [
        _line(shipped_quantity=5, sku_code="SKU-001"),
        _line(shipment_id=other_id, shipped_quantity=9, sku_code="SKU-009"),
        _line(shipment_id=other_id, line_no=2, shipped_quantity=1, sku_code="SKU-010"),
    ]
    service, _ = _service(
        rows=[_row(), _row(shipment_id=other_id, shipment_code="OCN-20260615")],
        lines=lines,
        total_count=2,
    )

    [first, second] = (await service.get_page(TENANT)).items

    assert (first.representative_sku_code, first.sku_count) == ("SKU-001", 1)
    assert (second.representative_sku_code, second.sku_count) == ("SKU-009", 2)


@pytest.mark.parametrize(
    ("stage", "expected", "actual", "is_delayed", "delay_days"),
    [
        # 입고 전: 예상 입고일 기준 (지연 위험 감지)
        ("AT_SEA", date(2026, 7, 6), None, False, 0),
        ("AT_SEA", date(2026, 7, 12), None, False, 0),
        ("PRE_DEPARTURE", date(2026, 7, 15), None, True, 3),
        # 입고 완료: 실제 입고일 기준 (사후 평가). 예상일이 달라도 실제 입고일을 본다
        ("RECEIVED", date(2026, 7, 12), date(2026, 7, 12), False, 0),
        ("RECEIVED", date(2026, 7, 14), date(2026, 7, 14), True, 2),
        ("RECEIVED", date(2026, 7, 20), date(2026, 7, 10), False, 0),
        ("RECEIVED", date(2026, 7, 10), date(2026, 7, 13), True, 1),
    ],
)
async def test_지연_여부와_일수는_입고_전엔_예상_입고일_입고_후엔_실제_입고일_기준이다(
    stage, expected, actual, is_delayed, delay_days
):
    row = _row(
        stage=stage, expected_3pl_inbound_date=expected, actual_3pl_inbound_date=actual
    )
    service, _ = _service(rows=[row], total_count=1)

    [item] = (await service.get_page(TENANT)).items

    assert (item.is_delayed, item.delay_days) == (is_delayed, delay_days)
