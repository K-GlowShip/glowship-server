from collections import Counter, defaultdict

import pytest
from sqlalchemy import text

from app.modules.delay_analysis.application.detection.episode_rules import (
    EpisodeAction,
    ScheduleObservation,
    decide_episode,
)

"""
    지연 에피소드 판정 재현 테스트 (실제 DB, 읽기 전용)
      - 확정 데이터의 선적 일정 스냅샷(223건)을 선적별 관측 순서대로 decide_episode 에 넣으면,
        확정 DB 의 delay_events(8건)와 AUTO 실행(19건: 생성 사유·원인 스냅샷)이 그대로 나와야 합니다.
"""


async def _replay(db):
    planned = dict(
        (
            await db.execute(
                text("SELECT shipment_id, planned_3pl_inbound_date FROM shipments")
            )
        ).all()
    )
    snapshots = defaultdict(list)
    for row in await db.execute(
        text(
            "SELECT shipment_schedule_snapshot_id AS id, shipment_id, observed_at, stage,"
            " expected_3pl_inbound_date FROM shipment_schedule_snapshots"
            " ORDER BY shipment_id, observed_at, shipment_schedule_snapshot_id"
        )
    ):
        snapshots[row.shipment_id].append(row)

    episodes, triggers = [], []
    for shipment_id, rows in snapshots.items():
        open_episode, previous, previous_row, number = None, None, None, 0
        for row in rows:
            current = ScheduleObservation(row.stage, row.expected_3pl_inbound_date)
            decision = decide_episode(
                planned_3pl_inbound_date=planned[shipment_id],
                current=current,
                previous=previous,
                episode_open=open_episode is not None,
            )
            if decision is not None:
                if decision.action == EpisodeAction.START:
                    number += 1
                    open_episode = {
                        "shipment_id": shipment_id,
                        "episode_no": number,
                        "detected": row.id,
                        "previous": previous_row.id if previous_row else None,
                        "detected_at": row.observed_at,
                        "closed_at": None,
                        "close_reason": None,
                    }
                    episodes.append(open_episode)
                if decision.trigger_reason is not None:
                    triggers.append(
                        (shipment_id, decision.trigger_reason.value, row.id)
                    )
                if decision.close_reason is not None:
                    open_episode["closed_at"] = row.observed_at
                    open_episode["close_reason"] = decision.close_reason.value
                    open_episode = None
            previous, previous_row = current, row
    return sum(map(len, snapshots.values())), episodes, triggers


async def test_replaying_snapshots_reproduces_confirmed_episodes_and_auto_runs(
    db_session,
):
    db = db_session
    total, episodes, triggers = await _replay(db)
    if total == 0:
        pytest.skip("일정 스냅샷이 없는 DB")

    confirmed = (
        await db.execute(
            text(
                "SELECT shipment_id, episode_no,"
                " detected_shipment_schedule_snapshot_id AS detected,"
                " previous_shipment_schedule_snapshot_id AS previous,"
                " detected_at, closed_at, close_reason FROM delay_events"
            )
        )
    ).all()
    confirmed_runs = (
        await db.execute(
            text(
                "SELECT e.shipment_id, r.trigger_reason, r.trigger_shipment_schedule_snapshot_id"
                " FROM analysis_runs r JOIN delay_events e USING (delay_event_id)"
                " WHERE r.trigger_type = 'AUTO'"
            )
        )
    ).all()

    if total == 223:  # 확정 2026 상반기 데이터
        assert (len(episodes), len(triggers)) == (8, 19)
    key = lambda e: (e["shipment_id"], e["episode_no"])
    assert sorted(episodes, key=key) == sorted(
        (dict(row._mapping) for row in confirmed), key=key
    )
    assert Counter(triggers) == Counter(tuple(row) for row in confirmed_runs)
