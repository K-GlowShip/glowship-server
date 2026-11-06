import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

from app.modules.data_sync.infrastructure.repository.data_sync_run_repository import (
    DataSyncRunRepository,
)

"""
    수동 수집 가상 시각 (GLOW-13·49, 2026-11-04 결정 "데모의 지금은 7월 초")
      - 데이터는 2026-06-30 까지이고 마지막 수집이 07-01 이다. 수동 수집(API 25)을 서버의 지금 시각(11월)으로 하면
        수집 시작·완료 시각이 11월이 되어 대시보드 기준 시각(C22)과 입고 추이·정시 입고율 기간이 11월로 옮겨가 화면이 빈다.
      - 그래서 수집 시각은 "그 원천의 마지막 적재 수집 시작 날짜 + 1일 00:00Z" 로 정한다. 처음(또는 그 값이 시작값보다
        이르면)은 Settings.virtual_clock_start(기본 2026-07-02T00:00Z)다. 누를 때마다 하루씩 나아간다.
      - 가상 TMS 에는 (지난 수집 시작, 이번 수집 시각] 의 응답을 묻는다(after < fetched_at ≤ until).
      - 수집 서비스의 now 는 이번 수집 시각으로 고정한다: 수집 시작·완료 시각과 일정 스냅샷 관측 시각이 모두 이 값이다.
      - 시작값이 없으면(VIRTUAL_CLOCK_START 비움) 서버의 지금 시각을 쓴다. 실제 원천을 붙이는 환경용이다.
"""


@dataclass(frozen=True)
class SyncWindow:
    # 지난 적재 수집 시작 시각. 이번에 받을 원천 응답은 이 시각 뒤의 것. 수집 이력이 없으면 None
    after: datetime | None
    # 이번 수집 시각 (수집 서비스 now)
    at: datetime


def next_virtual_sync_at(last_started_at: datetime | None, start: datetime) -> datetime:
    """마지막 적재 수집 시작 날짜(UTC) + 1일 00:00Z. 수집이 없거나 그 값이 start 보다 이르면 start"""
    if last_started_at is None:
        return start
    next_day = last_started_at.astimezone(UTC).date() + timedelta(days=1)
    return max(datetime.combine(next_day, time(0), tzinfo=UTC), start)


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
        self, company_id: uuid.UUID, source_domain: str
    ) -> SyncWindow:
        last = await self.repo.find_last_applied_started_at(company_id, source_domain)
        if self.virtual_start is None:
            return SyncWindow(after=last, at=self.now())
        return SyncWindow(after=last, at=next_virtual_sync_at(last, self.virtual_start))
