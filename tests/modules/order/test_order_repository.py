import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from types import SimpleNamespace

from sqlalchemy.dialects import postgresql

from app.modules.order.infrastructure.repository.order_repository import (
    OrderFilter,
    OrderLineRow,
    OrderListRow,
    OrderRepository,
    _like_pattern,
)

COMPANY_ID = uuid.uuid7()
ORDER_ID = uuid.uuid7()


@dataclass
class FakeSession:
    """실행한 문장과 파라미터를 기록한다. execute 는 행을, scalar 는 건수를 돌려준다."""

    rows: list = field(default_factory=list)
    total_count: int | None = 0
    executed: list = field(default_factory=list)

    async def execute(self, statement, params=None):
        self.executed.append((statement, params))
        return iter(self.rows)

    async def scalar(self, statement, params=None):
        self.executed.append((statement, params))
        return self.total_count


def _mapped(**values):
    """SQLAlchemy Row 처럼 _mapping 을 가진 객체"""
    return SimpleNamespace(_mapping=values)


def _order_row(**overrides):
    values = {
        "order_id": ORDER_ID,
        "order_code": "A-ORD-20260630-0177",
        "sales_channel": "AMAZON",
        "customer_region": "EAST",
        "ordered_at": datetime(2026, 6, 30, 23, 31, 48, tzinfo=UTC),
        "promised_delivery_at": datetime(2026, 7, 3, 23, 59, 59, tzinfo=UTC),
        "status": "OPEN",
        "secured_date": date(2026, 6, 29),
        "planned_dispatch_date": None,
        "actual_dispatch_date": None,
        "expected_delivery_date": None,
        "warehouse_id": uuid.uuid7(),
        "warehouse_code": "WH-EAST",
        "region_code": "EAST",
    }
    values.update(overrides)
    return _mapped(**values)


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


async def test_목록과_건수를_두_문장으로_조회하고_같은_필터를_넘긴다():
    session = FakeSession()
    filters = OrderFilter(
        status="OPEN",
        warehouse_id=uuid.UUID(int=1),
        sales_channel="AMAZON",
        ordered_from=datetime(2026, 6, 1, tzinfo=UTC),
        ordered_to=datetime(2026, 7, 1, tzinfo=UTC),
        query="ord_2026",
    )

    await OrderRepository(session).find_page(COMPANY_ID, filters, offset=40, limit=20)

    [(list_stmt, list_params), (count_stmt, count_params)] = session.executed
    expected = {
        "company_id": COMPANY_ID,
        "status": "OPEN",
        "warehouse_id": uuid.UUID(int=1),
        "sales_channel": "AMAZON",
        "ordered_from": datetime(2026, 6, 1, tzinfo=UTC),
        "ordered_to": datetime(2026, 7, 1, tzinfo=UTC),
        "pattern": "%ord\\_2026%",
    }
    assert list_params == {**expected, "offset": 40, "limit": 20}
    assert count_params == expected
    list_sql, count_sql = _sql(list_stmt), _sql(count_stmt)
    assert "ORDER BY ordered_at DESC, order_id" in list_sql
    assert "OFFSET" in list_sql and "LIMIT" in list_sql
    assert "SELECT COUNT(*) AS total_count FROM filtered" in count_sql


async def test_SQL_이_고객사_조건과_필터_통과_규칙과_SKU_검색을_담고_있다():
    session = FakeSession()

    await OrderRepository(session).find_page(
        COMPANY_ID, OrderFilter(), offset=0, limit=20
    )

    [(list_stmt, list_params), _] = session.executed
    sql = _sql(list_stmt)
    assert "o.company_id = %(company_id)s" in sql
    assert (
        "o.secured_date, o.planned_dispatch_date, o.actual_dispatch_date, o.expected_delivery_date"
        in sql
    )
    assert "JOIN warehouses w ON w.company_id = o.company_id" in sql
    for name in (
        "status",
        "warehouse_id",
        "sales_channel",
        "ordered_from",
        "ordered_to",
        "pattern",
    ):
        assert f"CAST(%({name})s" in sql
    assert sql.count("IS NULL") == 6
    assert "o.ordered_at >= %(ordered_from)s" in sql
    assert "o.ordered_at < %(ordered_to)s" in sql
    assert "o.order_code ILIKE %(pattern)s" in sql and "ESCAPE" in sql
    assert "k.sku_code ILIKE %(pattern)s" in sql
    assert "k.product_name ILIKE %(pattern)s" in sql
    assert "EXISTS (SELECT 1" in sql and "FROM order_lines l" in sql
    assert list_params["pattern"] is None


async def test_행을_OrderListRow_로_바꾸고_건수를_함께_돌려준다():
    session = FakeSession(rows=[_order_row()], total_count=7)

    page = await OrderRepository(session).find_page(
        COMPANY_ID, OrderFilter(), offset=0, limit=20
    )

    assert page.total_count == 7
    [row] = page.rows
    assert isinstance(row, OrderListRow)
    assert (row.order_id, row.order_code, row.status) == (
        ORDER_ID,
        "A-ORD-20260630-0177",
        "OPEN",
    )
    assert (row.warehouse_code, row.region_code) == ("WH-EAST", "EAST")
    assert row.secured_date == date(2026, 6, 29) and row.actual_dispatch_date is None


async def test_건수가_None_이면_0으로_본다():
    session = FakeSession(total_count=None)

    page = await OrderRepository(session).find_page(
        COMPANY_ID, OrderFilter(), offset=0, limit=20
    )

    assert page.total_count == 0


async def test_주문_ID_가_없으면_상세를_조회하지_않는다():
    session = FakeSession()

    lines = await OrderRepository(session).find_lines(COMPANY_ID, [])

    assert lines == []
    assert session.executed == []


async def test_상세는_주문_ID_배열로_한_번에_읽고_OrderLineRow_로_바꾼다():
    sku_id = uuid.uuid7()
    session = FakeSession(
        rows=[
            _mapped(
                order_id=ORDER_ID,
                line_no=1,
                sku_id=sku_id,
                sku_code="SKU-001",
                product_name="A Glow Hydra Toner Pad 60ea",
                ordered_quantity=3,
                shipped_quantity=1,
                line_status="ACTIVE",
            )
        ]
    )

    lines = await OrderRepository(session).find_lines(
        COMPANY_ID, (ORDER_ID, uuid.UUID(int=2))
    )

    [(stmt, params)] = session.executed
    assert params == {
        "company_id": COMPANY_ID,
        "order_ids": [ORDER_ID, uuid.UUID(int=2)],
    }
    sql = _sql(stmt)
    assert "l.order_id = ANY(%(order_ids)s::UUID[])" in sql
    assert "JOIN skus k ON k.company_id = l.company_id" in sql
    assert "ORDER BY l.order_id, l.line_no" in sql
    assert lines == [
        OrderLineRow(
            order_id=ORDER_ID,
            line_no=1,
            sku_id=sku_id,
            sku_code="SKU-001",
            product_name="A Glow Hydra Toner Pad 60ea",
            ordered_quantity=3,
            shipped_quantity=1,
            line_status="ACTIVE",
        )
    ]


def test_LIKE_특수문자는_글자_그대로_찾도록_이스케이프한다():
    assert _like_pattern(None) is None
    assert _like_pattern("ord") == "%ord%"
    assert _like_pattern("50%_a\\b") == "%50\\%\\_a\\\\b%"
