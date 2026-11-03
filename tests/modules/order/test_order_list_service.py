import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from app.core.pagination import PageParams
from app.core.security.tenant import TenantContext
from app.modules.order.application.exception.order_exceptions import (
    InvalidOrderedPeriodException,
)
from app.modules.order.application.order_list_service import OrderListService
from app.modules.order.infrastructure.repository.order_repository import (
    OrderFilter,
    OrderLineRow,
    OrderListPage,
    OrderListRow,
)

TENANT = TenantContext(user_id=uuid.uuid7(), company_id=uuid.uuid7())
ORDER_A = uuid.uuid7()
ORDER_B = uuid.uuid7()


@dataclass
class FakeOrderRepository:
    page: OrderListPage = field(
        default_factory=lambda: OrderListPage(rows=[], total_count=0)
    )
    lines: list[OrderLineRow] = field(default_factory=list)
    calls: list = field(default_factory=list)

    async def find_page(self, company_id, filters, **kwargs):
        self.calls.append(("find_page", company_id, filters, kwargs))
        return self.page

    async def find_lines(self, company_id, order_ids):
        self.calls.append(("find_lines", company_id, list(order_ids)))
        return self.lines


def _row(**overrides) -> OrderListRow:
    values = {
        "order_id": ORDER_A,
        "order_code": "A-ORD-20260630-0177",
        "sales_channel": "AMAZON",
        "customer_region": "EAST",
        "ordered_at": datetime(2026, 6, 30, 23, 31, 48, tzinfo=UTC),
        "promised_delivery_at": datetime(2026, 7, 3, 23, 59, 59, tzinfo=UTC),
        "status": "OPEN",
        "actual_dispatch_date": None,
        "warehouse_id": uuid.uuid7(),
        "warehouse_code": "WH-EAST",
        "region_code": "EAST",
    }
    values.update(overrides)
    return OrderListRow(**values)


def _line(order_id: uuid.UUID, line_no: int, **overrides) -> OrderLineRow:
    values = {
        "order_id": order_id,
        "line_no": line_no,
        "sku_id": uuid.uuid7(),
        "sku_code": f"SKU-00{line_no}",
        "product_name": "A Glow Hydra Toner Pad 60ea",
        "ordered_quantity": 3,
        "shipped_quantity": 0,
        "line_status": "ACTIVE",
    }
    values.update(overrides)
    return OrderLineRow(**values)


def _service(rows: list[OrderListRow], lines=(), total_count: int = 0):
    repo = FakeOrderRepository(
        page=OrderListPage(rows=rows, total_count=total_count), lines=list(lines)
    )
    return OrderListService(repo=repo), repo


async def test_필터가_없으면_빈_필터와_첫_페이지로_조회하고_상세는_조회하지_않는다():
    service, repo = _service(rows=[])

    result = await service.get_page(TENANT)

    [(_, company_id, filters, kwargs), (_, _, order_ids)] = repo.calls
    assert company_id == TENANT.company_id
    assert filters == OrderFilter()
    assert kwargs == {"offset": 0, "limit": 20}
    assert order_ids == []
    assert result.items == [] and result.total_count == 0
    assert (result.page.page, result.page.size) == (1, 20)


async def test_필터와_페이지를_그대로_넘기고_기간은_UTC_시각_그대로다():
    service, repo = _service(rows=[])

    await service.get_page(
        TENANT,
        status="OPEN",
        warehouse_id=uuid.UUID(int=1),
        sales_channel="AMAZON",
        ordered_from=datetime(2026, 5, 31, 15, 0, tzinfo=UTC),
        ordered_to=datetime(2026, 6, 30, 15, 0, tzinfo=UTC),
        query="  ord-2026  ",
        page=PageParams(page=3, size=50),
    )

    (_, _, filters, kwargs) = repo.calls[0]
    assert filters == OrderFilter(
        status="OPEN",
        warehouse_id=uuid.UUID(int=1),
        sales_channel="AMAZON",
        ordered_from=datetime(2026, 5, 31, 15, 0, tzinfo=UTC),
        ordered_to=datetime(2026, 6, 30, 15, 0, tzinfo=UTC),
        query="ord-2026",
    )
    assert kwargs == {"offset": 100, "limit": 50}


async def test_다른_시간대의_시각은_UTC_로_맞춘다():
    service, repo = _service(rows=[])
    kst = timezone(timedelta(hours=9))

    await service.get_page(
        TENANT,
        ordered_from=datetime(2026, 6, 1, 0, 0, tzinfo=kst),
        ordered_to=datetime(2026, 6, 2, 0, 0, tzinfo=kst),
    )

    filters = repo.calls[0][2]
    assert filters.ordered_from == datetime(2026, 5, 31, 15, 0, tzinfo=UTC)
    assert filters.ordered_to == datetime(2026, 6, 1, 15, 0, tzinfo=UTC)
    assert filters.ordered_from.tzinfo is UTC and filters.ordered_to.tzinfo is UTC


async def test_from_이_to_보다_늦으면_422_예외를_던지고_조회하지_않는다():
    service, repo = _service(rows=[])

    with pytest.raises(InvalidOrderedPeriodException) as error:
        await service.get_page(
            TENANT,
            ordered_from=datetime(2026, 7, 1, tzinfo=UTC),
            ordered_to=datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC),
        )

    assert error.value.error_code.status_code == 422
    assert error.value.error_code.code == "VALIDATION_ERROR"
    assert repo.calls == []


async def test_같은_시각_또는_한쪽만_있는_기간은_허용한다():
    service, repo = _service(rows=[])
    moment = datetime(2026, 6, 1, 15, 0, tzinfo=UTC)

    await service.get_page(TENANT, ordered_from=moment, ordered_to=moment)
    await service.get_page(TENANT, ordered_from=moment)
    await service.get_page(TENANT, ordered_to=moment)

    same, from_only, to_only = repo.calls[0][2], repo.calls[2][2], repo.calls[4][2]
    assert (same.ordered_from, same.ordered_to) == (moment, moment)
    assert from_only.ordered_to is None
    assert to_only.ordered_from is None


@pytest.mark.parametrize("query", [None, "", "   "])
async def test_검색어가_비어_있으면_없는_것으로_본다(query):
    service, repo = _service(rows=[])

    await service.get_page(TENANT, query=query)

    assert repo.calls[0][2].query is None


async def test_행을_OrderListItem_으로_바꾸고_상세_줄을_주문별로_묶는다():
    rows = [
        _row(order_id=ORDER_A),
        _row(
            order_id=ORDER_B,
            order_code="B",
            status="SHIPPED",
            actual_dispatch_date=date(2026, 6, 28),
        ),
    ]
    lines = [_line(ORDER_A, 1), _line(ORDER_A, 2), _line(uuid.uuid7(), 1)]
    service, repo = _service(rows=rows, lines=lines, total_count=2)

    result = await service.get_page(TENANT)

    assert repo.calls[1] == ("find_lines", TENANT.company_id, [ORDER_A, ORDER_B])
    [item_a, item_b] = result.items
    assert item_a.order_code == "A-ORD-20260630-0177"
    assert (item_a.warehouse_id, item_a.warehouse_code, item_a.region_code) == (
        rows[0].warehouse_id,
        "WH-EAST",
        "EAST",
    )
    assert item_a.actual_dispatch_date is None
    assert (item_b.order_code, item_b.actual_dispatch_date) == ("B", date(2026, 6, 28))
    # 배정 재생이 필요한 세 값은 아직 계산하지 않아 항상 null 이다 (D8, 3절)
    for item in (item_a, item_b):
        assert item.secured_date is None
        assert item.planned_dispatch_date is None
        assert item.expected_delivery_date is None
    # 상세 줄은 페이지의 주문 ID 로 한 번에 읽어 주문별로 묶는다. 없는 주문은 빈 튜플
    assert [line.line_no for line in item_a.lines] == [1, 2]
    assert (item_a.lines[0].sku_code, item_a.lines[0].ordered_quantity) == (
        "SKU-001",
        3,
    )
    assert item_b.lines == ()
    assert result.total_count == 2
