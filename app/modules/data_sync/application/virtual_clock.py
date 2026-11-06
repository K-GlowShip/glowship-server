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
