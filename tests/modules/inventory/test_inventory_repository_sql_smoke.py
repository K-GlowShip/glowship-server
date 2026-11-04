import uuid
from datetime import date

from sqlalchemy import text

from app.modules.inventory.infrastructure.repository.inventory_repository import (
    InventoryFilter,
    InventoryRepository,
)

"""
    InventoryRepository SQL 스모크 테스트 (실제 DB, 읽기 전용, 롤백)
      - 단위 테스트는 가짜 세션으로만 돌기 때문에, 여기서 SQL 자체가 실행되고 결과 형태가 맞는지 확인합니다.
      - 빈 DB 에서도 SQL 은 실행되어야 하므로, 데이터에 기대는 확인은 데이터가 있을 때만 합니다.
      - DB 가 없으면 db_session 픽스처가 건너뜁니다 (tests/conftest.py).
"""

UNKNOWN_ID = uuid.UUID("00000000-0000-7000-8000-000000000000")


async def _sample_company(db) -> uuid.UUID:
    """재고가 있는 고객사, 없으면 없는 ID"""
    company_id = await db.scalar(
        text(
            "SELECT company_id FROM inventory_daily ORDER BY inventory_date DESC LIMIT 1"
        )
    )
    return company_id or UNKNOWN_ID


async def test_빈_고객사는_최신_일자_None_과_빈_페이지_0건이다(db_session):
    repo = InventoryRepository(db_session)

    latest = await repo.find_latest_date(UNKNOWN_ID)
    page = await repo.find_page(
        UNKNOWN_ID,
        InventoryFilter(inventory_date=date(2026, 6, 30)),
        offset=0,
        limit=20,
    )

    assert latest is None
    assert page.rows == [] and page.total_count == 0


async def test_모든_필터를_걸어도_SQL_이_실행된다(db_session):
    filters = InventoryFilter(
        inventory_date=date(2026, 6, 30),
        warehouse_id=UNKNOWN_ID,
        sku_id=UNKNOWN_ID,
        query="50%_a\\b",
    )

    page = await InventoryRepository(db_session).find_page(
        UNKNOWN_ID, filters, offset=0, limit=20
    )

    assert page.rows == [] and page.total_count == 0


async def test_최신_일자는_DB_의_max_와_같고_그_일자의_목록은_정렬되고_조합이_하나씩이다(
    db_session,
):
    repo = InventoryRepository(db_session)
    company_id = await _sample_company(db_session)

    latest = await repo.find_latest_date(company_id)
    expected_latest = await db_session.scalar(
        text("SELECT max(inventory_date) FROM inventory_daily WHERE company_id = :c"),
        {"c": company_id},
    )
    assert latest == expected_latest
    if latest is None:
        return

    page = await repo.find_page(
        company_id, InventoryFilter(inventory_date=latest), offset=0, limit=100
    )
    beyond = await repo.find_page(
        company_id, InventoryFilter(inventory_date=latest), offset=10_000, limit=20
    )

    keys = [(row.warehouse_code, row.sku_code, row.sku_id) for row in page.rows]
    assert keys == sorted(keys)
    assert len(keys) == len(set(keys))
    assert page.total_count >= len(page.rows) >= 1
    assert beyond.rows == [] and beyond.total_count == page.total_count
    for row in page.rows:
        assert row.inventory_date == latest
        assert row.on_hand_quantity == row.reserved_quantity + row.available_quantity
        assert (
            min(row.reserved_quantity, row.available_quantity, row.outbound_quantity)
            >= 0
        )


async def test_창고_SKU_검색어_필터는_건수를_줄이고_조건에_맞는_행만_준다(db_session):
    repo = InventoryRepository(db_session)
    company_id = await _sample_company(db_session)
    latest = await repo.find_latest_date(company_id)
    if latest is None:
        return
    page = await repo.find_page(
        company_id, InventoryFilter(inventory_date=latest), offset=0, limit=100
    )
    sample = page.rows[0]

    by_warehouse = await repo.find_page(
        company_id,
        InventoryFilter(inventory_date=latest, warehouse_id=sample.warehouse_id),
        offset=0,
        limit=100,
    )
    by_sku = await repo.find_page(
        company_id,
        InventoryFilter(inventory_date=latest, sku_id=sample.sku_id),
        offset=0,
        limit=100,
    )
    by_name = await repo.find_page(
        company_id,
        InventoryFilter(inventory_date=latest, query=sample.product_name[:8].lower()),
        offset=0,
        limit=100,
    )
    other_day = await repo.find_page(
        company_id, InventoryFilter(inventory_date=date(1999, 1, 1)), offset=0, limit=20
    )

    assert 1 <= by_warehouse.total_count <= page.total_count
    assert all(row.warehouse_id == sample.warehouse_id for row in by_warehouse.rows)
    assert 1 <= by_sku.total_count <= page.total_count
    assert all(row.sku_id == sample.sku_id for row in by_sku.rows)
    assert (sample.warehouse_id, sample.sku_id) in {
        (row.warehouse_id, row.sku_id) for row in by_name.rows
    }
    assert other_day.rows == [] and other_day.total_count == 0
