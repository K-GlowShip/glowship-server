import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

from app.modules.data_sync.infrastructure.data_sync_enums import SourceDomain
from app.modules.data_sync.infrastructure.repository.data_sync_run_repository import (
    DataSyncRunRepository,
)

"""
    수동 수집 가상 시각 (GLOW-13·49, 2026-11-04 결정 "데모의 지금은 7월 초", 이슈 #39 규칙 2·3)
      - 데이터는 2026-06-30 까지이고 마지막 수집이 07-01 이다. 수동 수집(API 25)을 서버의 지금 시각(11월)으로 하면
        수집 시작·완료 시각이 11월이 되어 대시보드 기준 시각(C22)과 입고 추이·정시 입고율 기간이 11월로 옮겨가 화면이 빈다.
      - 그래서 수집 시각은 "그 원천의 마지막 적재 수집 시작 날짜 + 1일" 의 그 원천 자리(SOURCE_SLOT)로 정한다.
        처음(또는 그 날짜가 시작값 날짜보다 이르면)은 Settings.virtual_clock_start(기본 2026-07-02)의 날짜다.
        누를 때마다 하루씩 나아간다. 첫 SHIPMENT 수동 수집은 2026-07-02 00:03:00Z.
      - 원천 자리: overlay 데이터와 같게 원천마다 1분씩 띄운다(PRODUCT 00:00 … INVENTORY 00:04).
        엔진은 고객사 안에서 수집 시작 시각이 겹치지 않는다고 본다(store.run_at 이 (company_id, started_at) 키).
      - 완료 시각은 시작 + 30초다(VIRTUAL_FINISH_AFTER, overlay 와 같음). 수집 서비스 finish_after 로 넘긴다.
      - 가상 TMS 에는 (지난 적재 수집 날짜의 원천 자리, 이번 수집 시각] 의 응답을 묻는다(after < fetched_at ≤ until).
        구간 시작은 기록된 시작 시각이 아니라 자리다: 재시도·확정으로 시작이 밀려도 지난 구간의 끝과 이어진다.
        (실제 시각은 지난 적재 수집 시작 그대로. 시작이 밀리면 같은 틈이 생기지만 지금은 운영에서 쓰지 않는 경로라 후속)
      - 수집 서비스의 now 는 이번 수집 시각으로 고정한다: 수집 시작 시각과 일정 스냅샷 관측 시각이 이 값이다.
      - 시작값이 없으면(VIRTUAL_CLOCK_START 비움) 서버의 지금 시각을 쓴다. 실제 원천을 붙이는 환경용이다.
"""

# 원천별 가상 수집 시각 (분). overlay 데이터의 수집 시작 시각과 같다.
SOURCE_SLOT = {
    SourceDomain.PRODUCT: 0,
    SourceDomain.WAREHOUSE: 1,
    SourceDomain.ORDER: 2,
    SourceDomain.SHIPMENT: 3,
    SourceDomain.INVENTORY: 4,
}
VIRTUAL_FINISH_AFTER = timedelta(seconds=30)


@dataclass(frozen=True)
class SyncWindow:
    # 이번에 받을 원천 응답은 이 시각 뒤의 것. 가상 시각은 지난 적재 수집 날짜의 원천 자리, 실제 시각은 지난 적재 수집 시작.
    # 수집 이력이 없으면 None
    after: datetime | None
    # 이번 수집 시각 (수집 서비스 now)
    at: datetime


def next_virtual_sync_at(
    last_started_at: datetime | None, start: datetime, source_domain: SourceDomain
) -> datetime:
    """마지막 적재 수집 시작 날짜(UTC) + 1일의 원천 자리. 수집이 없거나 그 날짜가 시작값 날짜보다 이르면 시작값 날짜"""
    day = start.astimezone(UTC).date()
    if last_started_at is not None:
        day = max(day, last_started_at.astimezone(UTC).date() + timedelta(days=1))
    return datetime.combine(day, time(0, SOURCE_SLOT[source_domain]), tzinfo=UTC)


class SyncClock:
    def __init__(
        self,
        repo: DataSyncRunRepository,
        virtual_start: datetime | None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.repo = repo
        self.virtual_start = virtual_start
        self.now = now

    async def next_window(
        self, company_id: uuid.UUID, source_domain: SourceDomain
    ) -> SyncWindow:
        last = await self.repo.find_last_applied_started_at(
            company_id, source_domain.value
        )
        if self.virtual_start is None:
            return SyncWindow(after=last, at=self.now())
        return SyncWindow(
            after=virtual_window_after(last, source_domain),
            at=next_virtual_sync_at(last, self.virtual_start, source_domain),
        )


def virtual_window_after(
    last_started_at: datetime | None, source_domain: SourceDomain
) -> datetime | None:
    """
    가상 구간의 시작 = 지난 적재 수집 날짜의 원천 자리 = 지난 구간의 끝(until). 구간이 끊기지도 겹치지도 않는다.
    기록된 시작 시각을 쓰면, 같은 날 재시도(규칙 4)나 적용 때 확정(규칙 5)으로 시작이 자리보다 밀린 만큼
    (자리, 기록된 시작] 사이 응답이 어느 수집에도 들어오지 않는다(PR #52 rouiv 리뷰).
    """
    if last_started_at is None:
        return None
    day = last_started_at.astimezone(UTC).date()
    return datetime.combine(day, time(0, SOURCE_SLOT[source_domain]), tzinfo=UTC)


# --- 고객사 tick (2026-11-09 결정: 동기화 버튼 한 번에 다섯 원천을 함께, 버튼 한 번 = 가상 10분) ------------------------------
# 수집 순서 = 원천 자리 순서. 마스터(상품·창고)가 먼저라 주문·재고의 코드 확인이 맞고(UNKNOWN_SKU 방지),
# 시작 시각 순서 = 적용 순번(이슈 #39)이 원천을 누르는 순서와 상관없이 지켜진다.
TICK_ORDER = (
    SourceDomain.PRODUCT,
    SourceDomain.WAREHOUSE,
    SourceDomain.ORDER,
    SourceDomain.SHIPMENT,
    SourceDomain.INVENTORY,
)
SYNC_TICK = timedelta(minutes=10)


def tick_floor(value: datetime) -> datetime:
    """10분 격자로 내림 (UTC). 기록된 시작이 재시도·확정으로 몇 초 밀려도 같은 tick 으로 본다"""
    value = value.astimezone(UTC)
    return value.replace(
        minute=value.minute - value.minute % 10, second=0, microsecond=0
    )


def next_tick_at(last_company_started_at: datetime | None, start: datetime) -> datetime:
    """고객사의 다음 tick 시각 = 마지막 적재 수집이 속한 tick + 10분. 수집이 없거나 시작값보다 이르면 시작값의 tick"""
    first = tick_floor(start)
    if last_company_started_at is None:
        return first
    return max(first, tick_floor(last_company_started_at) + SYNC_TICK)


def tick_window(
    tick_at: datetime,
    last_domain_started_at: datetime | None,
    source_domain: SourceDomain,
) -> SyncWindow:
    """
    tick 의 원천 하나: 수집 시각 = tick + 원천 자리(분), 구간 = (그 원천이 지난번 적재된 tick + 자리, 이번 수집 시각].
    구간 시작을 기록된 시작이 아니라 격자로 잡아 틈·겹침이 없다(PR #52 리뷰와 같은 원리). 지난 tick 에서 그 원천만
    실패했으면 구간이 그만큼 넓어져 빠진 응답을 이번에 받는다.
    """
    slot = timedelta(minutes=SOURCE_SLOT[source_domain])
    after = (
        tick_floor(last_domain_started_at) + slot
        if last_domain_started_at is not None
        else None
    )
    return SyncWindow(after=after, at=tick_at + slot)


@dataclass(frozen=True)
class TickPlan:
    at: datetime  # tick 시각 (10분 격자, 가상 시각)
    windows: dict[SourceDomain, SyncWindow]


async def plan_tick(
    repo: DataSyncRunRepository,
    company_id: uuid.UUID,
    virtual_start: datetime | None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> TickPlan:
    """
    다음 tick 의 원천별 구간. L1 안에서 부른다(같은 tick 을 두 번 받지 않게).
    실제 시각(virtual_start 없음)이면 tick = 지금, 원천마다 1초씩 띄우고 구간 시작은 그 원천의 지난 적재 수집 시작이다.
    """
    lasts = {
        d: await repo.find_last_applied_started_at(company_id, d.value)
        for d in TICK_ORDER
    }
    if virtual_start is None:
        at = now().replace(microsecond=0)
        return TickPlan(
            at=at,
            windows={
                d: SyncWindow(after=lasts[d], at=at + timedelta(seconds=i))
                for i, d in enumerate(TICK_ORDER)
            },
        )
    at = next_tick_at(
        await repo.find_last_applied_started_at_of_company(company_id), virtual_start
    )
    return TickPlan(
        at=at, windows={d: tick_window(at, lasts[d], d) for d in TICK_ORDER}
    )


def next_midnight(tick_at: datetime) -> datetime:
    """'다음 날로'의 목표: tick 시각 다음의 자정(그 tick 이 자정이면 그 자정)"""
    day = tick_at.astimezone(UTC).date()
    midnight = datetime.combine(day, time(0), tzinfo=UTC)
    return midnight if tick_at == midnight else midnight + timedelta(days=1)
