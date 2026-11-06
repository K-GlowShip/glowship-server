import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy import text

from app.core.rls_context import set_rls_context
from app.modules.master_data.infrastructure.repository.sku_repository import (
    SkuRepository,
)

"""
    상품 상세 조회 SQL (실제 DB, 롤백)
      - 합성데이터의 최신 실행에는 ON_HOLD 가 없어, 롤백 트랜잭션에서 보류 실행을 그 지연의 최신 실행으로 만들고 지연을 연다.
      - 합성데이터의 SKU 는 모두 매핑이 있어, CODE_MISSING 은 매핑 행을 지워 재현한다.
      - DB 가 없으면 db_session·db_write_session 픽스처가 건너뜁니다 (tests/conftest.py).
"""

UNKNOWN_ID = uuid.UUID("00000000-0000-7000-8000-000000000000")


@dataclass(frozen=True)
class HeldSku:
    company_id: uuid.UUID
    delay_event_id: uuid.UUID
    shipment_id: uuid.UUID
    run_id: uuid.UUID
    sku_id: uuid.UUID


async def _held_sku(db, *, latest: bool = True, opened: bool = True) -> HeldSku:
    """ON_HOLD 실행의 지연과 그 선적의 SKU 하나. latest·opened 를 끄면 그 조건만 어긋난 상태로 둔다."""
    row = (
        await db.execute(
            text(
                "SELECT r.company_id, r.delay_event_id, e.shipment_id, r.analysis_run_id,"
                "       (SELECT l.sku_id FROM shipment_lines l WHERE l.shipment_id = e.shipment_id"
                "         ORDER BY l.line_no LIMIT 1) AS sku_id"
                "  FROM analysis_runs r JOIN delay_events e USING (delay_event_id)"
                " WHERE r.status = 'ON_HOLD' ORDER BY r.started_at LIMIT 1"
            )
        )
    ).first()
    if row is None:
        pytest.skip("ON_HOLD 실행이 있는 데이터가 없어 건너뜀")
    params = {"event": row.delay_event_id, "run": row.analysis_run_id}
    if latest:
        await db.execute(
            text(
                "UPDATE analysis_runs SET is_latest = false WHERE delay_event_id = :event"
            ),
            params,
        )
        await db.execute(
            text(
                "UPDATE analysis_runs SET is_latest = true WHERE analysis_run_id = :run"
            ),
            params,
        )
    closed_at = "closed_at = NULL, close_reason = NULL"
    if not opened:
        closed_at = "closed_at = detected_at, close_reason = 'RECEIVED'"
    await db.execute(
        text(f"UPDATE delay_events SET {closed_at} WHERE delay_event_id = :event"),
        params,
    )
    return HeldSku(
        row.company_id,
        row.delay_event_id,
        row.shipment_id,
        row.analysis_run_id,
        row.sku_id,
    )


async def _sku_with_open_issue(db):
    return (
        await db.execute(
            text(
                "SELECT k.company_id, k.sku_id, k.sku_code"
                "  FROM data_validation_issues d"
                "  JOIN skus k ON k.company_id = d.company_id"
                "   AND k.sku_code = split_part(d.source_record_key, '|', 2)"
                " WHERE d.status = 'OPEN' AND d.source_entity_type = 'INVENTORY' LIMIT 1"
            )
        )
    ).first()


async def test_없는_상품과_다른_고객사_상품은_None_이다(db_session):
    row = (
        await db_session.execute(text("SELECT company_id, sku_id FROM skus LIMIT 1"))
    ).first()
    if row is None:
        pytest.skip("상품이 없어 건너뜀")
    repo = SkuRepository(db_session)

    assert await repo.find_detail(row.company_id, UNKNOWN_ID) is None
    assert await repo.find_detail(UNKNOWN_ID, row.sku_id) is None


async def test_상세_행은_같은_상품의_목록_행과_같다(db_session):
    repo = SkuRepository(db_session)
    company_id = await db_session.scalar(text("SELECT company_id FROM skus LIMIT 1"))
    if company_id is None:
        pytest.skip("상품이 없어 건너뜀")

    page = await repo.find_page(
        company_id, query=None, only_errors=False, offset=0, limit=100
    )

    assert page.rows
    for row in page.rows:
        assert await repo.find_detail(company_id, row.sku_id) == row


async def test_열린_재고_이슈만_이_상품_코드로_찾는다(db_session):
    found = await _sku_with_open_issue(db_session)
    if found is None:
        pytest.skip("열린 재고 이슈가 없어 건너뜀")
    repo = SkuRepository(db_session)

    issues = await repo.find_open_issues(found.company_id, found.sku_code)

    assert issues
    assert all(i.source_record_key.split("|")[1] == found.sku_code for i in issues)
    assert await repo.find_open_issues(found.company_id, "NO-SUCH-SKU") == []
    assert await repo.find_open_issues(UNKNOWN_ID, found.sku_code) == []


async def test_해소된_이슈는_나오지_않는다(db_write_session):
    db = db_write_session
    found = await _sku_with_open_issue(db)
    if found is None:
        pytest.skip("열린 재고 이슈가 없어 건너뜀")
    await db.execute(
        text(
            "UPDATE data_validation_issues SET status = 'RESOLVED', resolved_at = detected_at"
            " WHERE company_id = :company AND status = 'OPEN' AND source_entity_type = 'INVENTORY'"
        ),
        {"company": found.company_id},
    )

    repo = SkuRepository(db)

    assert await repo.find_open_issues(found.company_id, found.sku_code) == []


async def test_상품이_포함된_열린_지연의_최신_보류_실행이_나온다(db_write_session):
    held = await _held_sku(db_write_session)

    rows = await SkuRepository(db_write_session).find_affected_delay_events(
        held.company_id, held.sku_id
    )

    assert [row.delay_event_id for row in rows] == [held.delay_event_id]
    assert rows[0].shipment_id == held.shipment_id
    assert rows[0].shipment_code
    assert rows[0].analysis_run_id == held.run_id
    assert rows[0].run_code
    assert rows[0].status == "ON_HOLD"
    assert rows[0].result_class is None
    assert rows[0].failure_code


async def test_다른_고객사_범위로는_영향_지연이_보이지_않는다(db_write_session):
    held = await _held_sku(db_write_session)

    rows = await SkuRepository(db_write_session).find_affected_delay_events(
        UNKNOWN_ID, held.sku_id
    )

    assert rows == []


async def test_선적에_없는_상품은_영향_지연이_없다(db_write_session):
    db = db_write_session
    held = await _held_sku(db)
    other = await db.scalar(
        text(
            "SELECT k.sku_id FROM skus k WHERE k.company_id = :company"
            " AND NOT EXISTS (SELECT 1 FROM shipment_lines l"
            "   WHERE l.shipment_id = :shipment AND l.sku_id = k.sku_id) LIMIT 1"
        ),
        {"company": held.company_id, "shipment": held.shipment_id},
    )
    if other is None:
        pytest.skip("선적에 없는 상품이 없어 건너뜀")

    assert (
        await SkuRepository(db).find_affected_delay_events(held.company_id, other) == []
    )


async def test_닫힌_지연은_나오지_않는다(db_write_session):
    held = await _held_sku(db_write_session, opened=False)

    rows = await SkuRepository(db_write_session).find_affected_delay_events(
        held.company_id, held.sku_id
    )

    assert rows == []


async def test_이전_버전의_보류_실행은_나오지_않는다(db_write_session):
    db = db_write_session
    held = await _held_sku(db, latest=False)
    assert not await db.scalar(
        text("SELECT is_latest FROM analysis_runs WHERE analysis_run_id = :run"),
        {"run": held.run_id},
    )

    rows = await SkuRepository(db).find_affected_delay_events(
        held.company_id, held.sku_id
    )

    assert rows == []


async def test_최신_실행이_보류가_아니면_나오지_않는다(db_write_session):
    db = db_write_session
    row = (
        await db.execute(
            text(
                "SELECT r.company_id, r.delay_event_id, l.sku_id"
                "  FROM analysis_runs r JOIN delay_events e USING (delay_event_id)"
                "  JOIN shipment_lines l ON l.shipment_id = e.shipment_id"
                " WHERE r.is_latest AND r.status <> 'ON_HOLD' LIMIT 1"
            )
        )
    ).first()
    if row is None:
        pytest.skip("보류가 아닌 최신 실행이 없어 건너뜀")
    await db.execute(
        text(
            "UPDATE delay_events SET closed_at = NULL, close_reason = NULL"
            " WHERE delay_event_id = :event"
        ),
        {"event": row.delay_event_id},
    )

    rows = await SkuRepository(db).find_affected_delay_events(
        row.company_id, row.sku_id
    )

    assert row.delay_event_id not in {r.delay_event_id for r in rows}


async def test_같은_상품의_선적_줄이_여러_개여도_지연은_한_건이다(db_write_session):
    db = db_write_session
    held = await _held_sku(db)
    await db.execute(
        text(
            "INSERT INTO shipment_lines"
            " (shipment_line_id, company_id, shipment_id, line_no, sku_id, shipped_quantity)"
            " VALUES (:id, :company, :shipment,"
            "  (SELECT max(line_no) + 1 FROM shipment_lines WHERE shipment_id = :shipment),"
            "  :sku, 1)"
        ),
        {
            "id": uuid.uuid7(),
            "company": held.company_id,
            "shipment": held.shipment_id,
            "sku": held.sku_id,
        },
    )

    rows = await SkuRepository(db).find_affected_delay_events(
        held.company_id, held.sku_id
    )

    assert len(rows) == 1


async def test_매핑이_없고_이슈가_없어도_영향_지연이_나온다(db_write_session):
    db = db_write_session
    held = await _held_sku(db)
    await db.execute(
        text(
            "DELETE FROM external_entity_mappings"
            " WHERE company_id = :company AND sku_id = :sku AND source_domain = 'INVENTORY'"
        ),
        {"company": held.company_id, "sku": held.sku_id},
    )
    repo = SkuRepository(db)

    detail = await repo.find_detail(held.company_id, held.sku_id)

    assert detail is not None
    assert detail.has_unmapped
    assert detail.external_codes[0].source_domain == "INVENTORY"
    assert detail.external_codes[0].external_code is None
    assert detail.external_codes[0].mapping_status == "UNMAPPED"
    assert await repo.find_open_issues(held.company_id, detail.sku_code) == []
    rows = await repo.find_affected_delay_events(held.company_id, held.sku_id)
    assert [row.delay_event_id for row in rows] == [held.delay_event_id]


async def test_앱_역할에서도_상품과_영향_지연이_읽힌다(db_write_session):
    # 운영 요청은 앱 역할이고 RLS 가 걸린다. 로컬 superuser 시험만으로는 분석 표가 조용히 0행이 되는 것을 놓친다
    db = db_write_session
    held = await _held_sku(db)
    user_id = await db.scalar(
        text(
            "SELECT user_id FROM users"
            " WHERE company_id = :company AND status = 'ACTIVE' LIMIT 1"
        ),
        {"company": held.company_id},
    )
    if user_id is None:
        pytest.skip("고객사의 ACTIVE 사용자가 없어 건너뜀")
    await db.execute(text("SET LOCAL ROLE glowship_v4_app"))
    await set_rls_context(db, user_id=user_id, company_id=held.company_id)
    repo = SkuRepository(db)

    assert await repo.find_detail(held.company_id, held.sku_id) is not None
    rows = await repo.find_affected_delay_events(held.company_id, held.sku_id)
    assert [row.delay_event_id for row in rows] == [held.delay_event_id]
