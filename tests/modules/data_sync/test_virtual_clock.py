import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.modules.data_sync.application.virtual_clock import (
    SOURCE_SLOT,
    VIRTUAL_FINISH_AFTER,
    SyncClock,
    SyncWindow,
    next_virtual_sync_at,
    virtual_window_after,
)
from app.modules.data_sync.infrastructure.data_sync_enums import SourceDomain

"""
    수동 수집 가상 시각 (2026-11-04 결정 "데모의 지금은 7월 초", 이슈 #39 규칙 2):
    마지막 적재 수집 날짜 + 1일의 원천 자리(SHIPMENT 00:03), 처음은 시작값 날짜
"""

START = datetime(2026, 7, 2, tzinfo=UTC)
COMPANY_ID = uuid.uuid7()
REAL_NOW = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)


JULY_2_SHIPMENT = datetime(2026, 7, 2, 0, 3, tzinfo=UTC)
JULY_3_SHIPMENT = datetime(2026, 7, 3, 0, 3, tzinfo=UTC)


@pytest.mark.parametrize(
    ("last", "expected"),
    [
        (None, JULY_2_SHIPMENT),
        # 확정 DB 의 마지막 SHIPMENT 수집 07-01 00:03Z → 07-02 00:03Z
        (datetime(2026, 7, 1, 0, 3, tzinfo=UTC), JULY_2_SHIPMENT),
        (JULY_2_SHIPMENT, JULY_3_SHIPMENT),
        # 같은 날 1초 밀린 수집(FAILED 뒤 재시도, 규칙 4) 뒤에도 다음 날 자리
        (datetime(2026, 7, 2, 0, 3, 1, tzinfo=UTC), JULY_3_SHIPMENT),
        (datetime(2026, 7, 2, 23, 59, tzinfo=UTC), JULY_3_SHIPMENT),
        # 날짜는 UTC 기준: 07-03 08:00+09:00 = 07-02 23:00Z
        (datetime.fromisoformat("2026-07-03T08:00:00+09:00"), JULY_3_SHIPMENT),
        # 시작값보다 이른 수집 뒤에도 시작값 날짜보다 앞으로 가지 않는다
        (datetime(2026, 6, 1, tzinfo=UTC), JULY_2_SHIPMENT),
    ],
)
def test_next_virtual_sync_at(last, expected):
    assert next_virtual_sync_at(last, START, SourceDomain.SHIPMENT) == expected


@pytest.mark.parametrize(
    ("domain", "minute"),
    [
        (SourceDomain.PRODUCT, 0),
        (SourceDomain.WAREHOUSE, 1),
        (SourceDomain.ORDER, 2),
        (SourceDomain.SHIPMENT, 3),
        (SourceDomain.INVENTORY, 4),
    ],
)
def test_each_source_has_its_own_minute_like_overlay(domain, minute):
    """이슈 #39 규칙 2: overlay 데이터와 같게 원천마다 1분씩 띄워 고객사 안에서 시작 시각이 겹치지 않는다"""
    assert next_virtual_sync_at(None, START, domain) == datetime(
        2026, 7, 2, 0, minute, tzinfo=UTC
    )
    assert SOURCE_SLOT.keys() == set(SourceDomain)
    assert VIRTUAL_FINISH_AFTER == timedelta(seconds=30)


@pytest.mark.parametrize(
    ("last", "expected"),
    [
        (None, None),
        (JULY_2_SHIPMENT, JULY_2_SHIPMENT),
        # 같은 날 재시도(규칙 4)·적용 때 확정(규칙 5)으로 밀린 시작도 그날 자리로 → 지난 구간 끝과 이어짐
        (datetime(2026, 7, 2, 0, 3, 1, tzinfo=UTC), JULY_2_SHIPMENT),
        (datetime(2026, 7, 2, 0, 7, 12, tzinfo=UTC), JULY_2_SHIPMENT),
    ],
)
def test_virtual_window_after_is_last_day_slot(last, expected):
    """PR #52 리뷰: 구간 시작 = 지난 적재 수집 날짜의 원천 자리 = 지난 구간의 until"""
    assert virtual_window_after(last, SourceDomain.SHIPMENT) == expected


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

    window = await clock.next_window(COMPANY_ID, SourceDomain.SHIPMENT)

    # 구간 시작은 지난 적재 수집 날짜의 원천 자리(07-02 00:03)
    assert window == SyncWindow(after=JULY_2_SHIPMENT, at=JULY_3_SHIPMENT)
    assert repo.calls == [(COMPANY_ID, "SHIPMENT")]


async def test_real_clock_when_virtual_start_is_unset():
    last = datetime(2026, 7, 2, tzinfo=UTC)
    clock = SyncClock(FakeRepository(last), None, now=lambda: REAL_NOW)

    assert await clock.next_window(COMPANY_ID, SourceDomain.SHIPMENT) == SyncWindow(
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
