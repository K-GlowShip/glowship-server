import uuid
from datetime import UTC, datetime

from sqlalchemy import text

from app.modules.decision.infrastructure.repository.decision_repository import (
    DecisionFilter,
    DecisionRepository,
)

"""
    DecisionRepository SQL 스모크 테스트 (실제 DB, 읽기 전용, 롤백)
      - 단위 테스트는 가짜 리포지토리로만 돌기 때문에, 여기서 UNION·조인·CASE 가 실제로 실행되고 결과 형태가 맞는지 확인합니다.
      - 빈 DB 에서도 SQL 은 실행되어야 하므로, 데이터에 기대는 확인은 데이터가 있을 때만 합니다.
      - 2026 H1 overlay(고객사 A 초안 2·최종 선택 2)가 적재되어 있으면 상태 3종·동점 정렬·필터 건수까지 확인합니다.
      - DB 가 없으면 db_session 픽스처가 건너뜁니다 (tests/conftest.py).
"""

UNKNOWN_ID = uuid.UUID("00000000-0000-7000-8000-000000000000")


async def _sample_company(db) -> uuid.UUID:
    """최종 선택이 있는 고객사, 없으면 없는 ID"""
    company_id = await db.scalar(
        text("SELECT company_id FROM final_decisions ORDER BY decided_at DESC LIMIT 1")
    )
    return company_id or UNKNOWN_ID


async def _count(db, sql: str, params: dict) -> int:
    return await db.scalar(text(sql), params)


async def test_빈_고객사는_빈_페이지와_0건이다(db_session):
    repo = DecisionRepository(db_session)

    page = await repo.find_page(UNKNOWN_ID, DecisionFilter(), offset=0, limit=20)
    lines = await repo.find_shipment_lines(UNKNOWN_ID, [UNKNOWN_ID])

    assert page.rows == [] and page.total_count == 0
    assert lines == []


async def test_모든_필터를_걸어도_SQL_이_실행된다(db_session):
    filters = DecisionFilter(
        decided_from=datetime(2026, 1, 1, tzinfo=UTC),
        decided_to=datetime(2026, 2, 1, tzinfo=UTC),
        decision_type="DRAFT",
        status="NEEDS_REANALYSIS",
        decided_by=UNKNOWN_ID,
        run_id=UNKNOWN_ID,
        query="50%_a\\b",
    )

    page = await DecisionRepository(db_session).find_page(
        UNKNOWN_ID, filters, offset=0, limit=20
    )

    assert page.rows == [] and page.total_count == 0


async def test_목록은_판단_시각_최신순이고_건수는_두_표의_합이다(db_session):
    repo = DecisionRepository(db_session)
    company_id = await _sample_company(db_session)

    page = await repo.find_page(company_id, DecisionFilter(), offset=0, limit=100)
    beyond = await repo.find_page(company_id, DecisionFilter(), offset=10_000, limit=20)

    expected = await _count(
        db_session,
        "SELECT (SELECT count(*) FROM member_drafts WHERE company_id = :c)"
        " + (SELECT count(*) FROM final_decisions WHERE company_id = :c)",
        {"c": company_id},
    )
    assert page.total_count == expected
    assert beyond.rows == [] and beyond.total_count == expected
    if not page.rows:
        return

    keys = [(row.decided_at, row.decision_id) for row in page.rows]
    # decided_at 내림차순, 같으면 decision_id 오름차순
    assert keys == sorted(keys, key=lambda k: (-k[0].timestamp(), k[1]))
    for row in page.rows:
        assert row.decision_type in ("DRAFT", "FINAL")
        assert row.status in ("DRAFT", "FINAL", "NEEDS_REANALYSIS")
        if row.decision_type == "FINAL":
            assert row.status == "FINAL"
            assert row.response_option_id is not None
            assert row.role_code == "CLIENT_LEAD"
        else:
            assert row.status == (
                "NEEDS_REANALYSIS" if row.run_status == "STALE" else "DRAFT"
            )
            assert row.role_code == "CLIENT_MEMBER"


async def test_상태_유형_선택자_실행_필터_건수가_직접_센_값과_같다(db_session):
    repo = DecisionRepository(db_session)
    company_id = await _sample_company(db_session)
    everything = await repo.find_page(company_id, DecisionFilter(), offset=0, limit=100)
    if not everything.rows:
        return

    async def total(**filters) -> int:
        page = await repo.find_page(
            company_id, DecisionFilter(**filters), offset=0, limit=100
        )
        return page.total_count

    rows = everything.rows
    assert await total(status="FINAL") == sum(r.status == "FINAL" for r in rows)
    assert await total(status="DRAFT") == sum(r.status == "DRAFT" for r in rows)
    assert await total(status="NEEDS_REANALYSIS") == sum(
        r.status == "NEEDS_REANALYSIS" for r in rows
    )
    assert await total(decision_type="DRAFT") == sum(
        r.decision_type == "DRAFT" for r in rows
    )
    first = rows[0]
    assert await total(decided_by=first.user_id) == sum(
        r.user_id == first.user_id for r in rows
    )
    assert await total(run_id=first.run_id) == sum(
        r.run_id == first.run_id for r in rows
    )
    assert await total(decided_by=UNKNOWN_ID) == 0
    assert await total(run_id=UNKNOWN_ID) == 0


async def test_검색어는_선적_코드와_선적의_SKU_로_찾는다(db_session):
    repo = DecisionRepository(db_session)
    company_id = await _sample_company(db_session)
    everything = await repo.find_page(company_id, DecisionFilter(), offset=0, limit=100)
    if not everything.rows:
        return
    first = everything.rows[0]

    by_code = await repo.find_page(
        company_id,
        DecisionFilter(query=first.shipment_code[:6].lower()),
        offset=0,
        limit=100,
    )
    assert first.decision_id in {r.decision_id for r in by_code.rows}

    sku_code = await db_session.scalar(
        text(
            "SELECT k.sku_code FROM shipment_lines l JOIN skus k ON k.sku_id = l.sku_id"
            " WHERE l.shipment_id = :s ORDER BY l.line_no LIMIT 1"
        ),
        {"s": first.shipment_id},
    )
    if sku_code:
        by_sku = await repo.find_page(
            company_id, DecisionFilter(query=sku_code), offset=0, limit=100
        )
        assert first.decision_id in {r.decision_id for r in by_sku.rows}

    none = await repo.find_page(
        company_id, DecisionFilter(query="no-such-code-xyz"), offset=0, limit=100
    )
    assert none.total_count == 0


async def test_기간_필터는_끝_시각을_포함하지_않는다(db_session):
    repo = DecisionRepository(db_session)
    company_id = await _sample_company(db_session)
    everything = await repo.find_page(company_id, DecisionFilter(), offset=0, limit=100)
    if not everything.rows:
        return
    moment = everything.rows[0].decided_at

    excluded = await repo.find_page(
        company_id,
        DecisionFilter(decided_from=moment, decided_to=moment),
        offset=0,
        limit=100,
    )
    included = await repo.find_page(
        company_id,
        DecisionFilter(decided_from=moment, decided_to=moment.replace(microsecond=1)),
        offset=0,
        limit=100,
    )

    assert excluded.total_count == 0
    assert included.total_count == sum(r.decided_at == moment for r in everything.rows)


async def test_페이지의_선적_상세_줄을_한_번에_읽는다(db_session):
    repo = DecisionRepository(db_session)
    company_id = await _sample_company(db_session)
    page = await repo.find_page(company_id, DecisionFilter(), offset=0, limit=100)
    if not page.rows:
        return
    shipment_ids = [r.shipment_id for r in page.rows]

    lines = await repo.find_shipment_lines(company_id, shipment_ids)

    assert {line.shipment_id for line in lines} <= set(shipment_ids)
    expected = await _count(
        db_session,
        "SELECT count(*) FROM shipment_lines WHERE company_id = :c AND shipment_id = ANY(:ids)",
        {"c": company_id, "ids": list(set(shipment_ids))},
    )
    assert len(lines) == expected
