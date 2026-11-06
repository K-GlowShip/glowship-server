import uuid
from datetime import UTC, datetime

import pytest

from app.core.config import Settings
from app.modules.data_sync.application.virtual_clock import (
    SyncClock,
    SyncWindow,
    next_virtual_sync_at,
)

"""
    수동 수집 가상 시각 (2026-11-04 결정 "데모의 지금은 7월 초"): 마지막 적재 수집 날짜 + 1일 00:00Z, 처음은 시작값
"""

START = datetime(2026, 7, 2, tzinfo=UTC)
COMPANY_ID = uuid.uuid7()
REAL_NOW = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("last", "expected"),
    [
        (None, START),
        # 확정 DB 의 마지막 SHIPMENT 수집 07-01 00:03Z → 07-02 00:00Z
        (datetime(2026, 7, 1, 0, 3, tzinfo=UTC), START),
        (datetime(2026, 7, 2, 0, 0, tzinfo=UTC), datetime(2026, 7, 3, tzinfo=UTC)),
        (datetime(2026, 7, 2, 23, 59, tzinfo=UTC), datetime(2026, 7, 3, tzinfo=UTC)),
        # 날짜는 UTC 기준: 07-03 08:00+09:00 = 07-02 23:00Z
        (
            datetime.fromisoformat("2026-07-03T08:00:00+09:00"),
            datetime(2026, 7, 3, tzinfo=UTC),
        ),
        # 시작값보다 이른 수집 뒤에도 시작값보다 앞으로 가지 않는다
        (datetime(2026, 6, 1, tzinfo=UTC), START),
    ],
)
def test_next_virtual_sync_at(last, expected):
    assert next_virtual_sync_at(last, START) == expected


class FakeRepository:
    def __init__(self, last):
        self.last = last
        self.calls = []

    async def find_last_applied_started_at(self, company_id, source_domain):
        self.calls.append((company_id, source_domain))
        return self.last


async def test_virtual_window_advances_one_day_from_last_applied_sync():
    last = datetime(2026, 7, 2, tzinfo=UTC)
    repo = FakeRepository(last)
    clock = SyncClock(repo, START, now=lambda: REAL_NOW)

    window = await clock.next_window(COMPANY_ID, "SHIPMENT")

    assert window == SyncWindow(after=last, at=datetime(2026, 7, 3, tzinfo=UTC))
    assert repo.calls == [(COMPANY_ID, "SHIPMENT")]


async def test_real_clock_when_virtual_start_is_unset():
    last = datetime(2026, 7, 2, tzinfo=UTC)
    clock = SyncClock(FakeRepository(last), None, now=lambda: REAL_NOW)

    assert await clock.next_window(COMPANY_ID, "SHIPMENT") == SyncWindow(
        after=last, at=REAL_NOW
    )


REQUIRED = {
    "postgres_host": "db",
    "postgres_port": 5432,
    "postgres_user": "u",
    "postgres_password": "p",
    "postgres_db": "d",
    "jwt_secret_key": "x" * 32,
}


def test_virtual_clock_start_defaults_to_early_july():
    assert Settings(_env_file=None, **REQUIRED).virtual_clock_start == START


def test_blank_virtual_clock_start_means_real_time():
    settings = Settings(_env_file=None, **REQUIRED, virtual_clock_start=" ")

    assert settings.virtual_clock_start is None


def test_virtual_clock_start_parses_iso_and_requires_timezone():
    settings = Settings(
        _env_file=None, **REQUIRED, virtual_clock_start="2026-07-05T00:00:00Z"
    )
    assert settings.virtual_clock_start == datetime(2026, 7, 5, tzinfo=UTC)

    with pytest.raises(ValueError, match="VIRTUAL_CLOCK_START"):
        Settings(_env_file=None, **REQUIRED, virtual_clock_start="2026-07-05T00:00:00")
