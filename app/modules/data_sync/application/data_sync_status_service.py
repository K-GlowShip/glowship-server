import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.security.tenant import TenantContext
from app.modules.data_sync.application.data_source_catalog import (
    DATA_SOURCES,
    DataSourceSpec,
)
from app.modules.data_sync.infrastructure.data_sync_enums import (
    APPLIED_STATUSES,
    SourceDomain,
)
from app.modules.data_sync.infrastructure.repository.data_sync_run_repository import (
    DataSyncRunRepository,
    LatestRunSummary,
)


@dataclass(frozen=True)
class DataSourceSyncStatus:
    """
    원천 유형 하나의 마지막 수집 상태. 응답 스키마와 같은 필드 이름이다.
      - 마지막 실행 필드(data_sync_run_id ~ rejected_count)는 수집 이력이 없으면 모두 None
      - is_synced·last_synced_at 은 명세 필드에 더한 서버 판정값(카드 배지·"마지막 수집" 표시용)
    """

    source_domain: SourceDomain
    source_system_name: str
    sync_interval_minutes: int
    data_sync_run_id: uuid.UUID | None
    connection_method: str | None
    status: str | None
    started_at: datetime | None
    finished_at: datetime | None
    received_count: int | None
    loaded_count: int | None
    rejected_count: int | None
    open_issue_count: int
    is_synced: bool
    last_synced_at: datetime | None


class DataSyncStatusService:
    """연동 데이터 조회 화면에 보여줄 원천 유형별 마지막 수집 상태를 만든다."""

    def __init__(
        self,
        repo: DataSyncRunRepository,
        stale_multiplier: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        virtual_clock: bool = False,
    ):
        """
        Args:
            repo: 수집 실행 이력 조회
            stale_multiplier: 수집 멈춤 판정 배수. 마지막 실행 시작이 수집 주기 × 배수보다 오래되면 동기화 안 됨으로 본다.
                0 이면 멈춤 판정을 하지 않는다 (Settings.sync_stale_multiplier).
            clock: 현재 시각(UTC). 테스트에서 시각을 고정하기 위한 주입점
            virtual_clock: True 면 지금 = 이 고객사의 가장 최근 수집 시작 시각(원천 무관, 수집 이력이 없으면 clock).
                데모는 데이터가 7월 초에 멈춰 있어 서버 시각(11월)으로 판정하면 모든 원천이 멈춘 것으로 보인다.
                가장 최근에 수집한 원천은 동기화됨, 그보다 주기 × 배수 넘게 뒤처진 원천은 동기화 안 됨이 된다
                (Settings.virtual_clock_start 가 있을 때, virtual_clock.py).
        """
        self.repo = repo
        self.stale_multiplier = stale_multiplier
        self.clock = clock
        self.virtual_clock = virtual_clock

    async def get_latest_statuses(
        self, tenant: TenantContext
    ) -> list[DataSourceSyncStatus]:
        """카탈로그 순서대로 원천 유형별 상태를 돌려준다. 수집 이력이 없는 원천도 항목에 포함된다."""
        summaries = await self.repo.find_latest_by_domain(
            tenant.company_id,
            [spec.source_domain.value for spec in DATA_SOURCES],
        )
        now = self._now(summaries.values())
        return [
            self._build_status(spec, summaries.get(spec.source_domain.value), now)
            for spec in DATA_SOURCES
        ]

    def _now(self, summaries) -> datetime:
        started = [summary.started_at for summary in summaries]
        if self.virtual_clock and started:
            return max(started)
        return self.clock()

    def _build_status(
        self, spec: DataSourceSpec, summary: LatestRunSummary | None, now: datetime
    ) -> DataSourceSyncStatus:
        return DataSourceSyncStatus(
            source_domain=spec.source_domain,
            source_system_name=spec.source_system_name,
            sync_interval_minutes=spec.sync_interval_minutes,
            data_sync_run_id=summary.data_sync_run_id if summary else None,
            connection_method=summary.connection_method if summary else None,
            status=summary.status if summary else None,
            started_at=summary.started_at if summary else None,
            finished_at=summary.finished_at if summary else None,
            received_count=summary.received_count if summary else None,
            loaded_count=summary.loaded_count if summary else None,
            rejected_count=summary.rejected_count if summary else None,
            open_issue_count=summary.open_issue_count if summary else 0,
            is_synced=self._is_synced(spec, summary, now),
            last_synced_at=summary.last_synced_at if summary else None,
        )

    def _is_synced(
        self, spec: DataSourceSpec, summary: LatestRunSummary | None, now: datetime
    ) -> bool:
        """
        동기화된 상태인지 판정한다. 두 조건을 모두 만족해야 True 다.
          1. 마지막으로 끝난 실행(RUNNING 제외)이 SUCCEEDED·PARTIAL_SUCCESS
             (진행 중 실행은 결과를 모르므로 직전에 끝난 실행으로 판정한다)
          2. 배수가 0 이 아니면, 가장 최근 실행 시작이 수집 주기 × 배수 이내
             (수집기가 멈춰 새 실행이 기록되지 않는 경우를 잡는다)
        수집 이력이 없으면 False 다.
        """
        if summary is None or summary.latest_completed_status not in APPLIED_STATUSES:
            return False

        if self.stale_multiplier == 0:
            return True
        allowed_gap = timedelta(
            minutes=spec.sync_interval_minutes * self.stale_multiplier
        )
        return now - summary.started_at <= allowed_gap
