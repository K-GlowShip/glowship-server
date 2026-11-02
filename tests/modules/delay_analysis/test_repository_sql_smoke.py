import uuid

from sqlalchemy import text

from app.modules.delay_analysis.application.stale.stale_service import (
    STALE_TARGET_STATUSES,
)
from app.modules.delay_analysis.infrastructure.repository.delay_event_read_repository import (
    DelayEventFilter,
    DelayEventReadRepository,
)
from app.modules.delay_analysis.infrastructure.repository.stale_change_repository import (
    StaleChangeRepository,
)

"""
    Repository SQL 스모크 테스트 (실제 DB, 읽기 전용, 롤백)
      - 서비스 테스트는 가짜 Repository 로만 돌기 때문에, 여기서 SQL 자체가 실행되고 결과 형태가 맞는지 확인합니다.
      - 데이터가 없는 DB(빈 v5 DB 등)에서도 SQL 은 실행되어야 하므로, 데이터에 기대는 확인은 데이터가 있을 때만 합니다.
      - DB 가 없으면 db_session 픽스처가 건너뜁니다 (tests/conftest.py).
"""

UNKNOWN_ID = uuid.UUID("00000000-0000-7000-8000-000000000000")


async def _sample_company(db) -> uuid.UUID:
    """지연이 있는 고객사, 없으면 아무 고객사, 그것도 없으면 없는 ID"""
    company_id = await db.scalar(
        text("SELECT company_id FROM delay_events ORDER BY detected_at DESC LIMIT 1")
    )
    company_id = company_id or await db.scalar(
        text("SELECT company_id FROM companies LIMIT 1")
    )
    return company_id or UNKNOWN_ID


# --- StaleChangeRepository -------------------------------------------------------


async def test_find_max_applied_seq(db_session):
    repo = StaleChangeRepository(db_session)
    company_id = await _sample_company(db_session)

    expected = await db_session.scalar(
        text(
            "SELECT COALESCE(MAX(applied_seq), 0) FROM data_sync_runs WHERE company_id = :c"
        ),
        {"c": company_id},
    )

    assert await repo.find_max_applied_seq(company_id) == expected
    assert await repo.find_max_applied_seq(UNKNOWN_ID) == 0


async def test_find_changes_after_returns_changes_in_seq_order(db_session):
    repo = StaleChangeRepository(db_session)
    company_id = await _sample_company(db_session)
    max_seq = await repo.find_max_applied_seq(company_id)
    after_seq = max(max_seq - 3, 0)

    changes = await repo.find_changes_after(company_id, after_seq, max_seq)

    seqs = [change.applied_seq for change in changes]
    assert seqs == sorted(seqs)
    assert all(after_seq < seq <= max_seq for seq in seqs)
    for change in changes:
        if change.entity_type == "INVENTORY":
            assert change.inventory_date is not None
        if change.entity_type in ("SHIPMENT", "SHIPMENT_LINE"):
            assert change.shipment_id is not None


async def test_find_target_run_ids_returns_only_open_latest_targets(db_session):
    repo = StaleChangeRepository(db_session)
    company_id = await _sample_company(db_session)
    max_seq = await repo.find_max_applied_seq(company_id)

    run_ids = await repo.find_target_run_ids(company_id, max_seq + 1)

    for run_id in run_ids:
        row = (
            await db_session.execute(
                text(
                    "SELECT a.status, a.is_latest, e.closed_at"
                    " FROM analysis_runs a JOIN delay_events e"
                    " ON e.company_id = a.company_id AND e.delay_event_id = a.delay_event_id"
                    " WHERE a.analysis_run_id = :r"
                ),
                {"r": run_id},
            )
        ).one()
        assert row.status in STALE_TARGET_STATUSES
        assert row.is_latest
        assert row.closed_at is None


async def test_find_closed_episode_run_ids_matches_delay_events(db_session):
    repo = StaleChangeRepository(db_session)
    run_ids = list(
        await db_session.scalars(text("SELECT analysis_run_id FROM analysis_runs"))
    )
    expected = set(
        await db_session.scalars(
            text(
                "SELECT a.analysis_run_id FROM analysis_runs a JOIN delay_events e"
                " ON e.company_id = a.company_id AND e.delay_event_id = a.delay_event_id"
                " WHERE e.closed_at IS NOT NULL"
            )
        )
    )

    assert await repo.find_closed_episode_run_ids(run_ids) == expected
    assert await repo.find_closed_episode_run_ids([]) == set()
    assert await repo.find_closed_episode_run_ids([UNKNOWN_ID]) == set()


async def test_find_delayed_shipment_skus(db_session):
    repo = StaleChangeRepository(db_session)
    event = (
        await db_session.execute(
            text(
                "SELECT company_id, delay_event_id, shipment_id FROM delay_events LIMIT 1"
            )
        )
    ).one_or_none()

    assert await repo.find_delayed_shipment_skus(UNKNOWN_ID, UNKNOWN_ID) is None
    if event is not None:
        shipment_id, skus = await repo.find_delayed_shipment_skus(
            event.company_id, event.delay_event_id
        )
        assert shipment_id == event.shipment_id
        assert skus == sorted(set(skus))


# --- DelayEventReadRepository ----------------------------------------------------


async def test_delay_event_counts_match_list_per_tab(db_session):
    repo = DelayEventReadRepository(db_session)
    company_id = await _sample_company(db_session)
    filters = DelayEventFilter()

    rows = await repo.find_delay_events(company_id, filters)
    counts = await repo.count_by_tab(company_id, filters)

    assert counts.all_count == len(rows)
    detected = [row.detected_at for row in rows]
    assert detected == sorted(detected, reverse=True)
    tab_statuses = {
        "pending_decision": ["READY_FOR_REVIEW"],
        "on_hold": ["ON_HOLD"],
        "needs_reanalysis": ["STALE", "FAILED"],
    }
    for field, statuses in tab_statuses.items():
        tab_rows = await repo.find_delay_events(
            company_id, DelayEventFilter(run_statuses=statuses)
        )
        assert getattr(counts, field) == len(tab_rows), field


async def test_delay_event_filters_execute(db_session):
    repo = DelayEventReadRepository(db_session)
    company_id = await _sample_company(db_session)

    for filters in (
        DelayEventFilter(q="a"),
        DelayEventFilter(warehouse_id=UNKNOWN_ID),
        DelayEventFilter(has_final_decision=False),
    ):
        await repo.find_delay_events(company_id, filters)

    assert await repo.find_delay_events(UNKNOWN_ID, DelayEventFilter()) == []


async def test_delay_event_detail_queries(db_session):
    repo = DelayEventReadRepository(db_session)
    company_id = await _sample_company(db_session)
    rows = await repo.find_delay_events(company_id, DelayEventFilter())

    assert await repo.find_delay_event(company_id, UNKNOWN_ID) is None
    if not rows:
        return
    row = await repo.find_delay_event(company_id, rows[0].delay_event_id)
    assert row == rows[0]
    lines = await repo.find_shipment_lines(company_id, [row.shipment_id])
    assert all(line.shipment_id == row.shipment_id for line in lines)
    assert await repo.find_shipment_lines(company_id, []) == []
    runs = await repo.find_runs(company_id, row.delay_event_id)
    assert [run.version_no for run in runs] == sorted(
        (run.version_no for run in runs), reverse=True
    )
    if row.previous_shipment_schedule_snapshot_id:
        assert (
            await repo.find_snapshot_expected_date(
                company_id, row.previous_shipment_schedule_snapshot_id
            )
            is not None
        )
