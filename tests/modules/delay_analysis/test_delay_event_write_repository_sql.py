import uuid
from datetime import UTC, datetime

from sqlalchemy import text

from app.modules.delay_analysis.infrastructure.repository.delay_event_write_repository import (
    DelayEventWriteRepository,
    NewDelayEvent,
    dedupe_key,
)

"""
    지연 에피소드 저장 리포지토리 SQL 테스트 (실제 DB, 끝나면 롤백)
      - 확정 DB 의 SHIPMENT 수집으로 초기 적재 판정·이번 수집 스냅샷·직전 스냅샷·선적별 에피소드 현황을 확인하고,
        롤백되는 트랜잭션 안에서 에피소드를 추가·종료해 DB 제약(ck6 dedupe_key, open_uq, ck7)을 통과하는지 본다.
      - DB 가 없으면 db_session·db_write_session 픽스처가 건너뜁니다 (tests/conftest.py).
"""

UNKNOWN_ID = uuid.UUID("00000000-0000-7000-8000-000000000000")


async def _shipment_runs(db, company_code: str):
    """고객사의 적재된 SHIPMENT 수집 (적용 순번 순)"""
    return (
        await db.execute(
            text(
                """
                SELECT r.company_id, r.data_sync_run_id
                  FROM data_sync_runs r JOIN companies c USING (company_id)
                 WHERE c.company_code = :code AND r.source_domain = 'SHIPMENT'
                   AND r.status IN ('SUCCEEDED', 'PARTIAL_SUCCESS')
                 ORDER BY r.applied_seq
                """
            ),
            {"code": company_code},
        )
    ).all()


async def test_첫_SHIPMENT_수집만_초기_적재다(db_write_session):
    """확정 DB 는 모든 수집이 적재된 상태라, 롤백 트랜잭션에서 첫 수집 뒤 SHIPMENT 수집을 FAILED 로 바꿔
    '첫 수집만 끝난 고객사'를 만든다. 그 첫 수집은 초기 적재(앞선 적재 수집 없음)이고, 다른 수집은 아니다."""
    db = db_write_session
    runs = await _shipment_runs(db, "A_BEAUTY")
    if len(runs) < 2:
        return  # 확정 데이터가 없는 DB
    first, later = runs[0], runs[1]
    repo = DelayEventWriteRepository(db)
    assert await repo.has_prior_applied_shipment_sync(
        first.company_id, later.data_sync_run_id
    )

    await db.execute(
        text(
            """
            UPDATE data_sync_runs SET status = 'FAILED', applied_seq = NULL
             WHERE company_id = :c AND source_domain = 'SHIPMENT' AND data_sync_run_id <> :first
            """
        ),
        {"c": first.company_id, "first": first.data_sync_run_id},
    )

    assert not await repo.has_prior_applied_shipment_sync(
        first.company_id, first.data_sync_run_id
    )
    assert await repo.has_prior_applied_shipment_sync(
        first.company_id, later.data_sync_run_id
    )  # 다음 수집에서 보면 첫 수집이 앞선 적재 수집
    assert not await repo.has_prior_applied_shipment_sync(UNKNOWN_ID, UNKNOWN_ID)


async def test_이번_수집_스냅샷과_직전_스냅샷을_읽는다(db_session):
    runs = await _shipment_runs(db_session, "A_BEAUTY")
    if not runs:
        return
    repo = DelayEventWriteRepository(db_session)
    checked = 0
    for run in runs[1:]:
        for snapshot in await repo.find_run_snapshots(
            run.company_id, run.data_sync_run_id
        ):
            expected_previous = (
                await db_session.execute(
                    text(
                        """
                        SELECT shipment_schedule_snapshot_id, stage, expected_3pl_inbound_date
                          FROM shipment_schedule_snapshots
                         WHERE shipment_id = :s AND observed_at < :at
                         ORDER BY observed_at DESC, shipment_schedule_snapshot_id DESC
                         LIMIT 1
                        """
                    ),
                    {"s": snapshot.shipment_id, "at": snapshot.observed_at},
                )
            ).one_or_none()
            assert snapshot.previous_snapshot_id == (
                expected_previous.shipment_schedule_snapshot_id
                if expected_previous
                else None
            )
            if expected_previous:
                assert snapshot.previous_stage == expected_previous.stage
            checked += 1
    assert checked > 0


async def test_선적별_마지막_번호와_열린_에피소드(db_session):
    rows = (
        await db_session.execute(
            text("SELECT company_id, shipment_id, episode_no FROM delay_events")
        )
    ).all()
    if not rows:
        return
    repo = DelayEventWriteRepository(db_session)
    company_id = rows[0].company_id
    shipment_ids = [r.shipment_id for r in rows if r.company_id == company_id]

    episodes = await repo.find_shipment_episodes(
        company_id, shipment_ids + [UNKNOWN_ID]
    )

    assert set(episodes) == set(shipment_ids)  # 에피소드 없는 선적은 결과에 없음
    assert all(
        e.open_delay_event_id is None for e in episodes.values()
    )  # 확정 DB 지연은 모두 닫힘
    assert await repo.find_shipment_episodes(company_id, []) == {}


async def test_에피소드를_추가하고_한_번만_닫는다(db_write_session):
    db = db_write_session
    snapshot = (
        await db.execute(
            text(
                """
                SELECT s.company_id, s.shipment_id, s.shipment_schedule_snapshot_id, s.observed_at,
                       sh.planned_3pl_inbound_date
                  FROM shipment_schedule_snapshots s
                  JOIN shipments sh ON sh.shipment_id = s.shipment_id
                 WHERE NOT EXISTS (
                       SELECT 1 FROM delay_events e
                        WHERE e.shipment_id = s.shipment_id AND e.closed_at IS NULL)
                 ORDER BY s.observed_at
                 LIMIT 1
                """
            )
        )
    ).one_or_none()
    if snapshot is None:
        return
    repo = DelayEventWriteRepository(db)
    episodes = await repo.find_shipment_episodes(
        snapshot.company_id, [snapshot.shipment_id]
    )
    number = episodes[snapshot.shipment_id].last_episode_no + 1 if episodes else 1
    event_id = uuid.uuid7()
    expected = snapshot.planned_3pl_inbound_date.replace(day=1).replace(
        year=snapshot.planned_3pl_inbound_date.year + 1
    )  # 계획일보다 늦은 날짜 (ck1)

    await repo.insert_delay_event(
        snapshot.company_id,
        NewDelayEvent(
            delay_event_id=event_id,
            shipment_id=snapshot.shipment_id,
            episode_no=number,
            detected_snapshot_id=snapshot.shipment_schedule_snapshot_id,
            previous_snapshot_id=None,
            stage="AT_SEA",
            planned_3pl_inbound_date=snapshot.planned_3pl_inbound_date,
            expected_3pl_inbound_date=expected,
            detected_at=snapshot.observed_at,
        ),
    )
    opened = await repo.find_shipment_episodes(
        snapshot.company_id, [snapshot.shipment_id]
    )
    closed_at = datetime(2027, 1, 1, tzinfo=UTC)
    await repo.close_delay_event(
        snapshot.company_id, event_id, closed_at=closed_at, close_reason="RECEIVED"
    )
    await repo.close_delay_event(
        snapshot.company_id,
        event_id,
        closed_at=datetime(2028, 1, 1, tzinfo=UTC),
        close_reason="RECOVERED",
    )  # 이미 닫혔으면 바꾸지 않음
    row = (
        await db.execute(
            text(
                "SELECT dedupe_key, episode_no, closed_at, close_reason FROM delay_events WHERE delay_event_id = :e"
            ),
            {"e": event_id},
        )
    ).one()

    assert opened[snapshot.shipment_id].open_delay_event_id == event_id
    assert row.dedupe_key == dedupe_key(snapshot.shipment_id, number)
    assert (row.episode_no, row.closed_at, row.close_reason) == (
        number,
        closed_at,
        "RECEIVED",
    )
