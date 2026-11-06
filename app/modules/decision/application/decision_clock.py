import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from app.modules.data_sync.infrastructure.repository.data_sync_run_repository import (
    DataSyncRunRepository,
)

"""
    판단 시각 (API 18 초안 createdAt, API 19 선택일·decidedAt, 11/05 결정)
      - 데모는 데이터가 2026-06-30 까지이고 수동 수집이 7월 초 가상 시각으로 돈다(Settings.virtual_clock_start, data_sync virtual_clock).
        판단 시각을 서버의 지금(11월)으로 쓰면 API 19 시간 확인에서 7월 약속일 주문이 모두 지나 TIME_ELAPSED 로 거부되고,
        판단 이력에서 초안(11월)과 수집·분석(7월)의 시각이 어긋난다.
      - 그래서 가상 시각이면 판단 시각 = 그 고객사의 가장 최근 수집 시작 시각(원천 무관)입니다. 수집 이력이 없으면 서버 시각.
      - 가상 시각이 아니면(VIRTUAL_CLOCK_START 비움) 서버의 지금 시각입니다.
"""


class DecisionClock:
    def __init__(
        self,
        runs: DataSyncRunRepository | None,
        virtual: bool,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.runs = runs
        self.virtual = virtual
        self._now = now

    @classmethod
    def fixed(cls, at: datetime) -> DecisionClock:
        """항상 같은 시각 (시험용)"""
        return cls(None, virtual=False, now=lambda: at)

    async def now(self, company_id: uuid.UUID) -> datetime:
        if self.virtual and self.runs is not None:
            latest = await self.runs.find_latest_started_at(company_id)
            if latest is not None:
                return latest
        return self._now()
