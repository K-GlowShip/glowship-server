import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.core.pagination import PageParams
from app.core.security.tenant import TenantContext
from app.modules.order.application.exception.order_exceptions import (
    InvalidOrderedPeriodException,
)
from app.modules.order.application.order_list_service import OrderListService
from app.modules.order.infrastructure.repository.order_repository import (
    OrderFilter,
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
    calls: list = field(default_factory=list)

    async def find_page(self, company_id, filters, **kwargs):
        self.calls.append(("find_page", company_id, filters, kwargs))
        return self.page


def _row(**overrides) -> OrderListRow:
    values = {
        "order_id": ORDER_A,
        "order_code": "A-ORD-20260630-0177",
        "sales_channel": "AMAZON",
        "customer_region": "EAST",
        "ordered_at": datetime(2026, 6, 30, 23, 31, 48, tzinfo=UTC),
        "promised_delivery_at": datetime(2026, 7, 3, 23, 59, 59, tzinfo=UTC),
        "status": "OPEN",
        "secured_date": None,
        "planned_dispatch_date": None,
        "actual_dispatch_date": None,
        "expected_delivery_date": None,
        "warehouse_id": uuid.uuid7(),
        "warehouse_code": "WH-EAST",
        "region_code": "EAST",
    }
    values.update(overrides)
    return OrderListRow(**values)


def _service(rows: list[OrderListRow], total_count: int = 0):
    repo = FakeOrderRepository(page=OrderListPage(rows=rows, total_count=total_count))
    return OrderListService(repo=repo), repo


async def test_필터가_없으면_빈_필터와_첫_페이지로_조회한다():
    service, repo = _service(rows=[])

    result = await service.get_page(TENANT)

    [(_, company_id, filters, kwargs)] = repo.calls
    assert company_id == TENANT.company_id
    assert filters == OrderFilter()
    assert kwargs == {"offset": 0, "limit": 20}
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


async def test_시간대가_없는_시각은_UTC_로_보고_다른_시간대는_UTC_로_맞춘다():
    service, repo = _service(rows=[])
    kst = timezone(timedelta(hours=9))

    await service.get_page(
        TENANT,
        ordered_from=datetime(2026, 6, 1, 0, 0),
        ordered_to=datetime(2026, 6, 2, 0, 0, tzinfo=kst),
    )

    filters = repo.calls[0][2]
    assert filters.ordered_from == datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    assert filters.ordered_to == datetime(2026, 6, 1, 15, 0, tzinfo=UTC)
    assert filters.ordered_to.tzinfo is UTC


@pytest.mark.parametrize("query", [None, "", "   "])
async def test_검색어가_비어_있으면_없는_것으로_본다(query):
    service, repo = _service(rows=[])

    await service.get_page(TENANT, query=query)

    assert repo.calls[0][2].query is None


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

    same, from_only, to_only = repo.calls[0][2], repo.calls[1][2], repo.calls[2][2]
    assert (same.ordered_from, same.ordered_to) == (moment, moment)
    assert from_only.ordered_to is None
    assert to_only.ordered_from is None


async def test_행을_OrderListItem_으로_바꾸고_창고_요약과_출고_날짜를_그대로_옮긴다():
    rows = [_row(order_id=ORDER_A), _row(order_id=ORDER_B, order_code="B")]
    service, _ = _service(rows=rows, total_count=2)

    result = await service.get_page(TENANT)

    [item_a, item_b] = result.items
    assert (item_a.order_id, item_a.order_code) == (ORDER_A, "A-ORD-20260630-0177")
    assert (item_a.warehouse_id, item_a.warehouse_code, item_a.region_code) == (
        rows[0].warehouse_id,
        "WH-EAST",
        "EAST",
    )
    assert item_a.secured_date is None and item_a.expected_delivery_date is None
    assert item_b.order_code == "B"
    assert result.total_count == 2
