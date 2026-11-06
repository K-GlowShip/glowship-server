import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.modules.data_sync.infrastructure.repository.data_sync_run_repository import (
    DataSyncRunRepository,
)
from app.modules.decision.application.decision_clock import DecisionClock

"""
    판단 시각 (11/05 결정): 가상 시각이면 고객사의 가장 최근 수집 시작 시각, 아니면 서버 시각
"""

COMPANY_ID = uuid.uuid7()
SERVER_NOW = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
LATEST_SYNC = datetime(2026, 7, 2, 0, 3, tzinfo=UTC)
UNKNOWN_ID = uuid.UUID("00000000-0000-7000-8000-000000000000")


class FakeRuns:
    def __init__(self, latest):
        self.latest = latest
        self.calls = []

    async def find_latest_started_at(self, company_id):
        self.calls.append(company_id)
        return self.latest


async def test_virtual_clock_uses_latest_sync_start():
    runs = FakeRuns(LATEST_SYNC)
    clock = DecisionClock(runs, virtual=True, now=lambda: SERVER_NOW)

    assert await clock.now(COMPANY_ID) == LATEST_SYNC
    assert runs.calls == [COMPANY_ID]


async def test_virtual_clock_without_sync_history_uses_server_time():
    clock = DecisionClock(FakeRuns(None), virtual=True, now=lambda: SERVER_NOW)

    assert await clock.now(COMPANY_ID) == SERVER_NOW


async def test_real_clock_ignores_sync_history():
    runs = FakeRuns(LATEST_SYNC)
    clock = DecisionClock(runs, virtual=False, now=lambda: SERVER_NOW)

    assert await clock.now(COMPANY_ID) == SERVER_NOW
    assert runs.calls == []


async def test_fixed_clock():
    assert await DecisionClock.fixed(LATEST_SYNC).now(COMPANY_ID) == LATEST_SYNC


async def test_latest_started_at_matches_db(db_session):
    rows = (
        await db_session.execute(
            text(
                "SELECT company_id, max(started_at) AS latest FROM data_sync_runs"
                " GROUP BY company_id"
            )
        )
    ).all()
    if not rows:
        pytest.skip("수집 이력이 없는 DB")
    runs = DataSyncRunRepository(db_session)

    for row in rows:
        assert await runs.find_latest_started_at(row.company_id) == row.latest
        clock = DecisionClock(runs, virtual=True, now=lambda: SERVER_NOW)
        # 확정 DB 는 7월 초 수집이 마지막이라 판단 시각이 11월이 아니다
        assert (await clock.now(row.company_id)).year == row.latest.year
    assert await runs.find_latest_started_at(UNKNOWN_ID) is None
