from datetime import UTC, datetime, timedelta

import pytest

from app.modules.data_sync.application.manual_sync_service import (
    ManualSyncService,
    TickTicket,
)
from app.modules.data_sync.application.virtual_clock import (
    TICK_ORDER,
    TickPlan,
    next_midnight,
    next_tick_at,
    tick_floor,
    tick_window,
)
from app.modules.data_sync.infrastructure.data_sync_enums import SourceDomain

"""
    고객사 tick 가상 시계 (2026-11-09 결정: 동기화 버튼 한 번에 다섯 원천을 함께, 버튼 한 번 = 가상 10분) — DB 없음
"""

START = datetime(2026, 7, 2, tzinfo=UTC)


def _t(text):
    return datetime.fromisoformat(text)


def test_tick_floor_ignores_seconds_pushed_by_retry():
    assert tick_floor(_t("2026-07-02T08:13:01Z")) == _t("2026-07-02T08:10:00Z")
    assert tick_floor(_t("2026-07-02T00:04:30Z")) == _t("2026-07-02T00:00:00Z")


@pytest.mark.parametrize(
    ("last", "expected"),
    [
        (None, "2026-07-02T00:00:00Z"),  # 수집 없음 → 시작값
        (
            "2026-07-01T00:04:00Z",
            "2026-07-02T00:00:00Z",
        ),  # 확정 DB(07-01) → 시작값이 더 늦음
        (
            "2026-07-02T00:04:00Z",
            "2026-07-02T00:10:00Z",
        ),  # 보완 하루(07-02 00:00~00:04) 다음
        ("2026-07-02T23:54:00Z", "2026-07-03T00:00:00Z"),  # 날이 바뀌는 tick
    ],
)
def test_next_tick_is_company_wide_ten_minutes(last, expected):
    assert next_tick_at(_t(last) if last else None, START) == _t(expected)


def test_window_starts_on_grid_plus_slot_and_widens_after_failure():
    tick = _t("2026-07-02T08:20:00Z")

    order = tick_window(tick, _t("2026-07-02T08:12:01Z"), SourceDomain.ORDER)
    failed_last_time = tick_window(tick, _t("2026-07-02T08:02:00Z"), SourceDomain.ORDER)
    first_ever = tick_window(tick, None, SourceDomain.INVENTORY)

    assert (order.after, order.at) == (
        _t("2026-07-02T08:12:00Z"),
        _t("2026-07-02T08:22:00Z"),
    )
    assert failed_last_time.after == _t(
        "2026-07-02T08:02:00Z"
    )  # 지난 tick 에 실패 → 구간이 넓어짐
    assert (first_ever.after, first_ever.at) == (None, _t("2026-07-02T08:24:00Z"))


def test_tick_order_is_masters_first():
    assert TICK_ORDER == (
        SourceDomain.PRODUCT,
        SourceDomain.WAREHOUSE,
        SourceDomain.ORDER,
        SourceDomain.SHIPMENT,
        SourceDomain.INVENTORY,
    )


@pytest.mark.parametrize(
    ("tick", "midnight"),
    [
        ("2026-07-02T08:10:00Z", "2026-07-03T00:00:00Z"),
        ("2026-07-03T00:00:00Z", "2026-07-03T00:00:00Z"),
    ],
)
def test_next_midnight(tick, midnight):
    assert next_midnight(_t(tick)) == _t(midnight)


async def test_run_until_midnight_runs_ticks_up_to_the_midnight_tick(monkeypatch):
    service = ManualSyncService(
        open_session=None, virtual_start=START, load_scenario=lambda: None
    )
    clock = {"at": _t("2026-07-02T23:30:00Z")}
    ran = []

    def ticket(at):
        return TickTicket(
            plan=TickPlan(at=at, windows={}), started=(), company_code="A"
        )

    async def request_tick(tenant):
        clock["at"] += timedelta(minutes=10)
        return ticket(clock["at"])

    async def run_tick(t):
        ran.append(t.plan.at)
        return []

    monkeypatch.setattr(service, "request_tick", request_tick)
    monkeypatch.setattr(service, "run_tick", run_tick)

    count = await service.run_until_midnight(None, ticket(clock["at"]))

    assert count == 4
    assert ran[0] == _t("2026-07-02T23:30:00Z") and ran[-1] == _t(
        "2026-07-03T00:00:00Z"
    )
