import uuid

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.modules.data_sync.application.virtual_clock import SyncClock
from app.modules.data_sync.infrastructure.data_sync_enums import SourceDomain
from app.modules.data_sync.infrastructure.repository.data_sync_run_repository import (
    DataSyncRunRepository,
)

"""
    가상 시각의 "마지막 적재 수집 시작" 조회 (실제 DB, 읽기 전용)
"""

UNKNOWN_ID = uuid.UUID("00000000-0000-7000-8000-000000000000")


async def test_last_applied_started_at_follows_applied_seq(db_session):
    rows = (
        await db_session.execute(
            text(
                "SELECT DISTINCT ON (company_id, source_domain) company_id, source_domain, started_at"
                " FROM data_sync_runs WHERE status IN ('SUCCEEDED', 'PARTIAL_SUCCESS')"
                " ORDER BY company_id, source_domain, applied_seq DESC"
            )
        )
    ).all()
    if not rows:
        pytest.skip("적재된 수집이 없는 DB")
    repo = DataSyncRunRepository(db_session)

    for row in rows:
        assert (
            await repo.find_last_applied_started_at(row.company_id, row.source_domain)
            == row.started_at
        )
    assert await repo.find_last_applied_started_at(UNKNOWN_ID, "SHIPMENT") is None


async def test_confirmed_db_first_virtual_sync_is_july_2nd(db_session):
    """확정 DB 는 마지막 SHIPMENT 수집이 07-01 이라 첫 수동 수집 시각이 07-02 00:03Z (SHIPMENT 자리, 이슈 #39 규칙 2)"""
    start = get_settings().virtual_clock_start
    if start is None:
        pytest.skip("VIRTUAL_CLOCK_START 를 비운 환경")
    company_ids = list(
        await db_session.scalars(
            text(
                "SELECT DISTINCT company_id FROM data_sync_runs"
                " WHERE source_domain = 'SHIPMENT' AND started_at::date = DATE '2026-07-01'"
            )
        )
    )
    if not company_ids:
        pytest.skip("확정 데이터가 없는 DB")
    clock = SyncClock(DataSyncRunRepository(db_session), start)

    for company_id in company_ids:
        window = await clock.next_window(company_id, SourceDomain.SHIPMENT)
        assert window.after.date().isoformat() == "2026-07-01"
        assert window.at.isoformat() == "2026-07-02T00:03:00+00:00"
