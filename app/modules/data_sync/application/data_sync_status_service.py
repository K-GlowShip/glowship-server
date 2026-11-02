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
class ExternalSystemSyncStatus:
    """원천 유형 하나의 동기화 상태. 응답 스키마와 같은 필드 이름이다."""

    source_domain: SourceDomain
    source_system_name: str
    sync_interval_minutes: int
    is_synced: bool
    last_synced_at: datetime | None


class DataSyncStatusService:
    """연동 데이터 조회 화면에 보여줄 원천 유형별 동기화 상태를 만든다."""

    def __init__(
        self,
        repo: DataSyncRunRepository,
        stale_multiplier: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        """
        Args:
            repo: 수집 실행 이력 조회
            stale_multiplier: 수집 멈춤 판정 배수. 마지막 실행 시작이 수집 주기 × 배수보다 오래되면 동기화 안 됨으로 본다.
                0 이면 멈춤 판정을 하지 않는다 (Settings.sync_stale_multiplier).
            clock: 현재 시각(UTC). 테스트에서 시각을 고정하기 위한 주입점
        """
        self.repo = repo
        self.stale_multiplier = stale_multiplier
        self.clock = clock

    async def get_latest_statuses(
        self, tenant: TenantContext
    ) -> list[ExternalSystemSyncStatus]:
        """카탈로그 순서대로 원천 유형별 상태를 돌려준다. 수집 이력이 없는 원천도 항목에 포함된다."""
        summaries = await self.repo.find_latest_by_domain(
            tenant.company_id,
            [spec.source_domain.value for spec in DATA_SOURCES],
        )
        now = self.clock()
        return [
            self._build_status(spec, summaries.get(spec.source_domain.value), now)
            for spec in DATA_SOURCES
        ]

    def _build_status(
        self, spec: DataSourceSpec, summary: LatestRunSummary | None, now: datetime
    ) -> ExternalSystemSyncStatus:
        return ExternalSystemSyncStatus(
            source_domain=spec.source_domain,
            source_system_name=spec.source_system_name,
            sync_interval_minutes=spec.sync_interval_minutes,
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
