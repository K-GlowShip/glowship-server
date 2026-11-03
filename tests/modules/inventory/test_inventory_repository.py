import uuid
from dataclasses import dataclass, field
from datetime import date
from types import SimpleNamespace

from sqlalchemy.dialects import postgresql

from app.modules.inventory.infrastructure.repository.inventory_repository import (
    InventoryFilter,
    InventoryRepository,
    InventoryRow,
    _like_pattern,
)

COMPANY_ID = uuid.uuid7()
DAY = date(2026, 6, 30)


@dataclass
class FakeSession:
    """실행한 문장과 파라미터를 기록한다. execute 는 행을, scalar 는 단일 값을 돌려준다."""

    rows: list = field(default_factory=list)
    scalar_value: object = 0
    executed: list = field(default_factory=list)

    async def execute(self, statement, params=None):
        self.executed.append((statement, params))
        return iter(self.rows)

    async def scalar(self, statement, params=None):
        self.executed.append((statement, params))
        return self.scalar_value


def _mapped(**values):
    """SQLAlchemy Row 처럼 _mapping 을 가진 객체"""
    return SimpleNamespace(_mapping=values)


def _inventory_row(**overrides):
    values = {
        "inventory_date": DAY,
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
    return _mapped(**values)


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


async def test_최신_일자는_고객사_조건으로_max_를_읽는다():
    session = FakeSession(scalar_value=DAY)

    latest = await InventoryRepository(session).find_latest_date(COMPANY_ID)

    [(stmt, params)] = session.executed
    sql = _sql(stmt)
    assert "max(inventory_date)" in sql and "company_id = %(company_id)s" in sql
    assert params == {"company_id": COMPANY_ID}
    assert latest == DAY


async def test_적재된_재고가_없으면_최신_일자는_None_이다():
    session = FakeSession(scalar_value=None)

    assert await InventoryRepository(session).find_latest_date(COMPANY_ID) is None


async def test_목록과_건수를_두_문장으로_조회하고_같은_필터를_넘긴다():
    session = FakeSession()
    filters = InventoryFilter(
        inventory_date=DAY,
        warehouse_id=uuid.UUID(int=1),
        sku_id=uuid.UUID(int=2),
        query="sku_0",
    )

    await InventoryRepository(session).find_page(
        COMPANY_ID, filters, offset=20, limit=20
    )

    [(list_stmt, list_params), (count_stmt, count_params)] = session.executed
    expected = {
        "company_id": COMPANY_ID,
        "inventory_date": DAY,
        "warehouse_id": uuid.UUID(int=1),
        "sku_id": uuid.UUID(int=2),
        "pattern": "%sku\\_0%",
    }
    assert list_params == {**expected, "offset": 20, "limit": 20}
    assert count_params == expected
    list_sql, count_sql = _sql(list_stmt), _sql(count_stmt)
    assert "ORDER BY warehouse_code, sku_code, sku_id" in list_sql
    assert "OFFSET" in list_sql and "LIMIT" in list_sql
    assert "SELECT COUNT(*) AS total_count FROM filtered" in count_sql


async def test_SQL_이_고객사_일자_조건과_필터_통과_규칙과_조인을_담고_있다():
    session = FakeSession()

    await InventoryRepository(session).find_page(
        COMPANY_ID, InventoryFilter(inventory_date=DAY), offset=0, limit=20
    )

    [(list_stmt, list_params), _] = session.executed
    sql = _sql(list_stmt)
    assert "i.company_id = %(company_id)s" in sql
    assert "i.inventory_date = %(inventory_date)s" in sql
    assert "JOIN warehouses w ON w.company_id = i.company_id" in sql
    assert "JOIN skus k ON k.company_id = i.company_id" in sql
    for name in ("warehouse_id", "sku_id", "pattern"):
        assert f"CAST(%({name})s" in sql
    assert sql.count("IS NULL") == 3
    assert "k.sku_code ILIKE %(pattern)s" in sql
    assert "k.product_name ILIKE %(pattern)s" in sql
    assert list_params["pattern"] is None
    assert list_params["warehouse_id"] is None and list_params["sku_id"] is None


async def test_행을_InventoryRow_로_바꾸고_건수를_함께_돌려준다():
    session = FakeSession(rows=[_inventory_row()], scalar_value=66)

    page = await InventoryRepository(session).find_page(
        COMPANY_ID, InventoryFilter(inventory_date=DAY), offset=0, limit=20
    )

    assert page.total_count == 66
    [row] = page.rows
    assert isinstance(row, InventoryRow)
    assert (row.inventory_date, row.warehouse_code, row.sku_code) == (
        DAY,
        "WH-WEST",
        "SKU-001",
    )
    assert (row.on_hand_quantity, row.reserved_quantity, row.available_quantity) == (
        1240,
        310,
        930,
    )


async def test_건수가_None_이면_0으로_본다():
    session = FakeSession(scalar_value=None)

    page = await InventoryRepository(session).find_page(
        COMPANY_ID, InventoryFilter(inventory_date=DAY), offset=0, limit=20
    )

    assert page.total_count == 0


def test_LIKE_특수문자는_글자_그대로_찾도록_이스케이프한다():
    assert _like_pattern(None) is None
    assert _like_pattern("toner") == "%toner%"
    assert _like_pattern("50%_a\\b") == "%50\\%\\_a\\\\b%"
