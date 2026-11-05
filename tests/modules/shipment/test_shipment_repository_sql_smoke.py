import uuid

from sqlalchemy import text

from app.modules.shipment.infrastructure.repository.shipment_repository import (
    ShipmentFilter,
    ShipmentRepository,
)

"""
    ShipmentRepository SQL 스모크 테스트 (실제 DB, 읽기 전용, 롤백)
      - 단위 테스트는 가짜 세션으로만 돌기 때문에, 여기서 SQL 자체가 실행되고 결과 형태가 맞는지 확인합니다.
      - 빈 DB 에서도 SQL 은 실행되어야 하므로, 데이터에 기대는 확인은 데이터가 있을 때만 합니다.
      - DB 가 없으면 db_session 픽스처가 건너뜁니다 (tests/conftest.py).
"""

UNKNOWN_ID = uuid.UUID("00000000-0000-7000-8000-000000000000")


async def _sample_company(db) -> uuid.UUID:
    """선적이 있는 고객사, 없으면 없는 ID"""
    company_id = await db.scalar(
        text(
            "SELECT company_id FROM shipments ORDER BY planned_3pl_inbound_date DESC LIMIT 1"
        )
    )
    return company_id or UNKNOWN_ID


async def test_빈_고객사는_빈_페이지와_0건이다(db_session):
    repo = ShipmentRepository(db_session)

    page = await repo.find_page(UNKNOWN_ID, ShipmentFilter(), offset=0, limit=20)
    lines = await repo.find_lines(UNKNOWN_ID, [UNKNOWN_ID])

    assert page.rows == [] and page.total_count == 0
    assert lines == []


async def test_모든_필터를_걸어도_SQL_이_실행된다(db_session):
    filters = ShipmentFilter(
        stage="AT_SEA",
        transport_mode="OCEAN",
        warehouse_id=UNKNOWN_ID,
        query="50%_a\\b",
    )

    page = await ShipmentRepository(db_session).find_page(
        UNKNOWN_ID, filters, offset=0, limit=20
    )

    assert page.rows == [] and page.total_count == 0


async def test_목록은_계획_입고일_최신순이고_건수는_필터와_맞는다(db_session):
    repo = ShipmentRepository(db_session)
    company_id = await _sample_company(db_session)

    page = await repo.find_page(company_id, ShipmentFilter(), offset=0, limit=100)
    beyond = await repo.find_page(company_id, ShipmentFilter(), offset=10_000, limit=20)

    keys = [(row.planned_3pl_inbound_date, row.shipment_id) for row in page.rows]
    assert keys == sorted(keys, key=lambda key: (-key[0].toordinal(), key[1]))
    assert page.total_count >= len(page.rows)
    assert beyond.rows == [] and beyond.total_count == page.total_count
    for row in page.rows:
        # RECEIVED ⇔ 실제 입고일 있음 (shipments_ck3)
        assert (row.stage == "RECEIVED") == (row.actual_3pl_inbound_date is not None)
        assert row.us_port_name

    if not page.rows:
        return
    sample = page.rows[0]
    narrowed = await repo.find_page(
        company_id,
        ShipmentFilter(
            stage=sample.stage,
            transport_mode=sample.transport_mode,
            warehouse_id=sample.warehouse_id,
            query=sample.shipment_code[:6].lower(),
        ),
        offset=0,
        limit=100,
    )
    assert 1 <= narrowed.total_count <= page.total_count
    assert sample.shipment_id in {row.shipment_id for row in narrowed.rows}
    assert all(
        (row.stage, row.transport_mode, row.warehouse_id)
        == (sample.stage, sample.transport_mode, sample.warehouse_id)
        for row in narrowed.rows
    )


async def test_페이지의_상세_줄_수는_shipment_lines_건수와_같고_줄_번호_순이다(
    db_session,
):
    repo = ShipmentRepository(db_session)
    company_id = await _sample_company(db_session)
    page = await repo.find_page(company_id, ShipmentFilter(), offset=0, limit=20)
    if not page.rows:
        return
    shipment_ids = [row.shipment_id for row in page.rows]

    lines = await repo.find_lines(company_id, shipment_ids)

    expected_count = await db_session.scalar(
        text(
            "SELECT count(*) FROM shipment_lines"
            " WHERE company_id = :company_id AND shipment_id = ANY(:shipment_ids)"
        ),
        {"company_id": company_id, "shipment_ids": shipment_ids},
    )
    assert len(lines) == expected_count
    assert all(line.shipment_id in set(shipment_ids) for line in lines)
    keys = [(line.shipment_id, line.line_no) for line in lines]
    assert keys == sorted(keys)
    assert all(line.shipped_quantity > 0 for line in lines)
