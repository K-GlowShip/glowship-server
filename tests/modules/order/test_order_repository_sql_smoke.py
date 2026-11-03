import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.modules.order.infrastructure.repository.order_repository import (
    OrderFilter,
    OrderRepository,
)

"""
    OrderRepository SQL 스모크 테스트 (실제 DB, 읽기 전용, 롤백)
      - 단위 테스트는 가짜 세션으로만 돌기 때문에, 여기서 SQL 자체가 실행되고 결과 형태가 맞는지 확인합니다.
      - 빈 DB 에서도 SQL 은 실행되어야 하므로, 데이터에 기대는 확인은 데이터가 있을 때만 합니다.
      - DB 가 없으면 db_session 픽스처가 건너뜁니다 (tests/conftest.py).
"""

UNKNOWN_ID = uuid.UUID("00000000-0000-7000-8000-000000000000")


async def _sample_company(db) -> uuid.UUID:
    """주문이 있는 고객사, 없으면 없는 ID"""
    company_id = await db.scalar(
        text("SELECT company_id FROM orders ORDER BY ordered_at DESC LIMIT 1")
    )
    return company_id or UNKNOWN_ID


async def test_빈_고객사는_빈_페이지와_0건이다(db_session):
    repo = OrderRepository(db_session)

    page = await repo.find_page(UNKNOWN_ID, OrderFilter(), offset=0, limit=20)
    lines = await repo.find_lines(UNKNOWN_ID, [UNKNOWN_ID])

    assert page.rows == [] and page.total_count == 0
    assert lines == []


async def test_모든_필터를_걸어도_SQL_이_실행된다(db_session):
    filters = OrderFilter(
        status="OPEN",
        warehouse_id=UNKNOWN_ID,
        sales_channel="AMAZON",
        ordered_from=datetime(2026, 6, 1, tzinfo=UTC),
        ordered_to=datetime(2026, 7, 1, tzinfo=UTC),
        query="50%_a\\b",
    )

    page = await OrderRepository(db_session).find_page(
        UNKNOWN_ID, filters, offset=0, limit=20
    )

    assert page.rows == [] and page.total_count == 0


async def test_목록은_주문일시_최신순이고_건수는_필터와_맞는다(db_session):
    repo = OrderRepository(db_session)
    company_id = await _sample_company(db_session)

    page = await repo.find_page(company_id, OrderFilter(), offset=0, limit=100)
    beyond = await repo.find_page(
        company_id, OrderFilter(), offset=10_000_000, limit=20
    )

    keys = [(row.ordered_at, row.order_id) for row in page.rows]
    assert keys == sorted(keys, key=lambda key: (-key[0].timestamp(), key[1]))
    assert page.total_count >= len(page.rows)
    assert beyond.rows == [] and beyond.total_count == page.total_count

    if not page.rows:
        return
    sample = page.rows[0]
    narrowed = await repo.find_page(
        company_id,
        OrderFilter(
            status=sample.status,
            warehouse_id=sample.warehouse_id,
            sales_channel=sample.sales_channel,
            ordered_from=sample.ordered_at,
            ordered_to=sample.ordered_at + timedelta(seconds=1),
        ),
        offset=0,
        limit=100,
    )
    assert 1 <= narrowed.total_count <= page.total_count
    assert sample.order_id in {row.order_id for row in narrowed.rows}
    assert all(
        (row.status, row.warehouse_id, row.sales_channel)
        == (sample.status, sample.warehouse_id, sample.sales_channel)
        for row in narrowed.rows
    )


async def test_실제_출고일은_첫_ORDER_LINE_UPDATE_수집일_빼기_1과_같다(db_session):
    """D8 역산 결과를 독립된 SQL(주문별 전체 집계)과 대조한다. 출고 전(변경 기록 없음) 주문은 null 이어야 한다."""
    repo = OrderRepository(db_session)
    company_id = await _sample_company(db_session)
    page = await repo.find_page(company_id, OrderFilter(), offset=0, limit=100)
    if not page.rows:
        return

    expected = {
        row.order_id: row.actual_dispatch_date
        for row in (
            await db_session.execute(
                text(
                    "SELECT l.order_id,"
                    " MIN((r.started_at AT TIME ZONE 'UTC')::date) - 1 AS actual_dispatch_date"
                    " FROM order_lines l"
                    " JOIN data_change_records d ON d.entity_type = 'ORDER_LINE'"
                    " AND d.change_type = 'UPDATE' AND d.entity_ref = l.order_line_id"
                    " JOIN data_sync_runs r ON r.data_sync_run_id = d.data_sync_run_id"
                    " WHERE l.company_id = :company_id AND l.order_id = ANY(:order_ids)"
                    " GROUP BY l.order_id"
                ),
                {
                    "company_id": company_id,
                    "order_ids": [row.order_id for row in page.rows],
                },
            )
        ).all()
    }

    for row in page.rows:
        assert row.actual_dispatch_date == expected.get(row.order_id)
        if row.actual_dispatch_date is not None:
            assert row.actual_dispatch_date >= row.ordered_at.date() - timedelta(days=1)


async def test_검색어는_주문_번호와_SKU_코드와_상품명을_찾는다(db_session):
    repo = OrderRepository(db_session)
    company_id = await _sample_company(db_session)
    page = await repo.find_page(company_id, OrderFilter(), offset=0, limit=5)
    if not page.rows:
        return
    sample = page.rows[0]
    [line, *_] = await repo.find_lines(company_id, [sample.order_id])

    by_code = await repo.find_page(
        company_id, OrderFilter(query=sample.order_code.lower()), offset=0, limit=20
    )
    by_sku = await repo.find_page(
        company_id,
        OrderFilter(
            query=line.sku_code.lower(),
            ordered_from=sample.ordered_at,
            ordered_to=sample.ordered_at + timedelta(seconds=1),
        ),
        offset=0,
        limit=20,
    )

    by_name = await repo.find_page(
        company_id,
        OrderFilter(
            query=line.product_name[:8].lower(),
            ordered_from=sample.ordered_at,
            ordered_to=sample.ordered_at + timedelta(seconds=1),
        ),
        offset=0,
        limit=20,
    )

    assert sample.order_id in {row.order_id for row in by_code.rows}
    assert sample.order_id in {row.order_id for row in by_sku.rows}
    assert sample.order_id in {row.order_id for row in by_name.rows}


async def test_상세는_페이지의_주문에만_속하고_줄_번호_순이다(db_session):
    repo = OrderRepository(db_session)
    company_id = await _sample_company(db_session)
    page = await repo.find_page(company_id, OrderFilter(), offset=0, limit=20)
    order_ids = [row.order_id for row in page.rows]

    lines = await repo.find_lines(company_id, order_ids)

    assert {line.order_id for line in lines} == set(order_ids)
    for order_id in order_ids:
        numbers = [line.line_no for line in lines if line.order_id == order_id]
        assert numbers == sorted(numbers) and numbers[0] >= 1
    for line in lines:
        assert line.sku_code and line.product_name
        assert 0 <= line.shipped_quantity <= line.ordered_quantity
