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

    분석 실행 시각 (API 20 재분석, 이슈 #50 결정 11/06)
      - 가상 시각이면 실행 시각 = 그 고객사의 마지막 "적재" 수집 시작 시각(초 단위). 판단 시각처럼 상태 무관 최근 수집을 쓰면,
        진행 중(RUNNING) 수집의 임시 시작 시각으로 실행을 만들 수 있다. 그 수집이 같은 시각으로 확정 적재되면 엔진은
        "시작 ≤ 실행 시각"이라 그 수집을 입력에 넣는데 실행 input_seq 에는 없어 PR #36 check_input_seq 가 멈춘다.
        마지막 적재 수집 시작이면 실행 시각 = 입력 마지막 수집 시작이고, 그 뒤 수집은 이슈 #39 규칙 5 로 반드시 더 늦게 시작한다.
      - 판단 시각(상태 무관 최근 수집) ≥ 이 값이라 "판단 ≥ 실행 시작"도 지켜진다.
      - 가상 시각이 아니면 None: create_analysis_run 이 L1 뒤 DB 시각을 쓴다(지금과 같음).
      - L1 을 잡은 뒤 불러야 한다(그 사이 적재된 수집을 놓치지 않게, create_analysis_run 이 이르면 거부).
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

    async def run_started_at(self, company_id: uuid.UUID) -> datetime | None:
        """분석 실행 시각. 가상 시각이면 고객사 마지막 적재 수집 시작, 아니면(또는 적재 이력이 없으면) None(= DB 시각)"""
        if self.virtual and self.runs is not None:
            return await self.runs.find_last_applied_started_at_of_company(company_id)
        return None
