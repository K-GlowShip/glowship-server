import uuid
from dataclasses import dataclass, field
from datetime import date
from types import SimpleNamespace

from sqlalchemy.dialects import postgresql

from app.modules.shipment.infrastructure.repository.shipment_repository import (
    ShipmentFilter,
    ShipmentLineRow,
    ShipmentListRow,
    ShipmentRepository,
    _like_pattern,
)

COMPANY_ID = uuid.uuid7()
SHIPMENT_ID = uuid.uuid7()


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


def _shipment_row(**overrides):
    values = {
        "shipment_id": SHIPMENT_ID,
        "shipment_code": "OCN-20260601",
        "transport_mode": "OCEAN",
        "stage": "AT_SEA",
        "planned_kr_release_date": date(2026, 6, 17),
        "planned_3pl_inbound_date": date(2026, 7, 12),
        "expected_3pl_inbound_date": date(2026, 7, 6),
        "actual_3pl_inbound_date": None,
        "warehouse_id": uuid.uuid7(),
        "warehouse_code": "WH-WEST",
        "region_code": "WEST",
        "us_port_name": "LOS ANGELES, CA",
        "origin_region_code": "KR",
    }
    values.update(overrides)
    return _mapped(**values)


def _line_row(**overrides):
    values = {
        "shipment_id": SHIPMENT_ID,
        "line_no": 1,
        "sku_id": uuid.uuid7(),
        "shipped_quantity": 100,
        "sku_code": "SKU-001",
        "product_name": "A Glow Hydra Toner Pad 60ea",
    }
    values.update(overrides)
    return _mapped(**values)


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


async def test_목록과_건수를_두_문장으로_조회하고_같은_필터를_넘긴다():
    session = FakeSession()
    filters = ShipmentFilter(
        stage="AT_SEA",
        transport_mode="OCEAN",
        warehouse_id=uuid.UUID(int=1),
        query="ocn_2026",
    )

    await ShipmentRepository(session).find_page(
        COMPANY_ID, filters, offset=20, limit=20
    )

    [(list_stmt, list_params), (count_stmt, count_params)] = session.executed
    expected = {
        "company_id": COMPANY_ID,
        "stage": "AT_SEA",
        "transport_mode": "OCEAN",
        "warehouse_id": uuid.UUID(int=1),
        "pattern": "%ocn\\_2026%",
    }
    assert list_params == {**expected, "offset": 20, "limit": 20}
    assert count_params == expected
    list_sql, count_sql = _sql(list_stmt), _sql(count_stmt)
    assert "ORDER BY planned_3pl_inbound_date DESC, shipment_id" in list_sql
    assert "OFFSET" in list_sql and "LIMIT" in list_sql
    assert "SELECT COUNT(*) AS total_count FROM filtered" in count_sql


async def test_SQL_이_고객사_조건과_필터_통과_규칙과_조인과_현재_값을_담고_있다():
    session = FakeSession()

    await ShipmentRepository(session).find_page(
        COMPANY_ID, ShipmentFilter(), offset=0, limit=20
    )

    [(list_stmt, list_params), _] = session.executed
    sql = _sql(list_stmt)
    assert "s.company_id = %(company_id)s" in sql
    assert "JOIN warehouses w ON w.company_id = s.company_id" in sql
    assert "JOIN market_leadtime_references m" in sql
    assert "o.warehouse_type = 'KR_ORIGIN'" in sql
    assert "s.current_stage AS stage" in sql
    assert "s.current_expected_3pl_inbound_date AS expected_3pl_inbound_date" in sql
    for name in ("stage", "transport_mode", "warehouse_id", "pattern"):
        assert f"CAST(%({name})s" in sql
    assert sql.count("IS NULL") == 4
    assert "s.shipment_code ILIKE %(pattern)s" in sql
    assert "sku_code" not in sql and "product_name" not in sql
    assert list_params["pattern"] is None
    assert list_params["stage"] is None and list_params["transport_mode"] is None
    assert list_params["warehouse_id"] is None


async def test_행을_ShipmentListRow_로_바꾸고_건수를_함께_돌려준다():
    session = FakeSession(rows=[_shipment_row()], scalar_value=83)

    page = await ShipmentRepository(session).find_page(
        COMPANY_ID, ShipmentFilter(), offset=0, limit=20
    )

    assert page.total_count == 83
    [row] = page.rows
    assert isinstance(row, ShipmentListRow)
    assert (row.shipment_code, row.stage, row.us_port_name, row.origin_region_code) == (
        "OCN-20260601",
        "AT_SEA",
        "LOS ANGELES, CA",
        "KR",
    )
    assert row.actual_3pl_inbound_date is None


async def test_건수가_None_이면_0으로_본다():
    session = FakeSession(scalar_value=None)

    page = await ShipmentRepository(session).find_page(
        COMPANY_ID, ShipmentFilter(), offset=0, limit=20
    )

    assert page.total_count == 0


async def test_상세는_선적_ID_배열로_한_번에_읽고_선적_줄_번호_순이다():
    other_id = uuid.uuid7()
    session = FakeSession(rows=[_line_row(), _line_row(shipment_id=other_id)])

    lines = await ShipmentRepository(session).find_lines(
        COMPANY_ID, [SHIPMENT_ID, other_id]
    )

    [(stmt, params)] = session.executed
    sql = _sql(stmt)
    assert "l.shipment_id = ANY(%(shipment_ids)s::UUID[])" in sql
    assert "JOIN skus k ON k.company_id = l.company_id" in sql
    assert "ORDER BY l.shipment_id, l.line_no" in sql
    assert params == {"company_id": COMPANY_ID, "shipment_ids": [SHIPMENT_ID, other_id]}
    assert all(isinstance(line, ShipmentLineRow) for line in lines)
    assert [line.shipment_id for line in lines] == [SHIPMENT_ID, other_id]


async def test_선적_ID_가_없으면_상세를_조회하지_않는다():
    session = FakeSession()

    lines = await ShipmentRepository(session).find_lines(COMPANY_ID, [])

    assert lines == [] and session.executed == []


def test_LIKE_특수문자는_글자_그대로_찾도록_이스케이프한다():
    assert _like_pattern(None) is None
    assert _like_pattern("ocn") == "%ocn%"
    assert _like_pattern("50%_a\\b") == "%50\\%\\_a\\\\b%"
