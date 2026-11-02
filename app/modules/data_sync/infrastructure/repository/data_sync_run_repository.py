import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ARRAY, UUID, Text, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

"""
    외부 데이터 수집 실행 이력 조회
      - 연동 데이터 조회 화면은 도메인별로 "가장 최근 실행", "마지막으로 끝난 실행", "마지막으로 적재된 실행"
        세 가지를 다른 용도로 쓴다. 한 번의 SQL 로 세 가지를 도메인별 한 행에 모은다.
          latest    : 상태와 무관한 가장 최근 실행 → started_at (수집이 멈췄는지 판정)
          completed : RUNNING 을 뺀 가장 최근 실행 → status (동기화 성공 여부 판정)
          applied   : SUCCEEDED·PARTIAL_SUCCESS 중 applied_seq 최대 → finished_at (마지막 수집 시각)
      - 운영에서는 RLS 가 고객사 범위를 강제하지만, 조건을 명시해 로컬(superuser)에서도 같은 결과가 나오게 한다.
"""

_LATEST_BY_DOMAIN = text(
    """
    WITH latest AS (
        SELECT DISTINCT ON (source_domain) source_domain, started_at
          FROM data_sync_runs
         WHERE company_id = :company_id AND source_domain = ANY(:source_domains)
         ORDER BY source_domain, started_at DESC, data_sync_run_id DESC
    ),
    completed AS (
        SELECT DISTINCT ON (source_domain) source_domain, status
          FROM data_sync_runs
         WHERE company_id = :company_id AND source_domain = ANY(:source_domains)
           AND status <> 'RUNNING'
         ORDER BY source_domain, started_at DESC, data_sync_run_id DESC
    ),
    applied AS (
        SELECT DISTINCT ON (source_domain) source_domain, finished_at
          FROM data_sync_runs
         WHERE company_id = :company_id AND source_domain = ANY(:source_domains)
           AND status IN ('SUCCEEDED', 'PARTIAL_SUCCESS')
         ORDER BY source_domain, applied_seq DESC
    )
    SELECT l.source_domain,
           l.started_at AS latest_started_at,
           c.status AS latest_completed_status,
           a.finished_at AS last_synced_at
      FROM latest l
      LEFT JOIN completed c ON c.source_domain = l.source_domain
      LEFT JOIN applied a ON a.source_domain = l.source_domain
    """
).bindparams(
    bindparam("company_id", type_=UUID(as_uuid=True)),
    bindparam("source_domains", type_=ARRAY(Text())),
)


@dataclass(frozen=True)
class LatestRunSummary:
    """도메인 하나의 최신 수집 요약. 수집 이력이 한 건이라도 있으면 latest_started_at 은 항상 있다."""

    latest_started_at: datetime
    latest_completed_status: str | None
    last_synced_at: datetime | None


class DataSyncRunRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # Read
    async def find_latest_by_domain(
        self, company_id: uuid.UUID, source_domains: Iterable[str]
    ) -> dict[str, LatestRunSummary]:
        """고객사의 도메인별 최신 수집 요약. 수집 이력이 없는 도메인은 결과에 없다."""
        result = await self.db.execute(
            _LATEST_BY_DOMAIN,
            {"company_id": company_id, "source_domains": list(source_domains)},
        )
        return {
            row.source_domain: LatestRunSummary(
                latest_started_at=row.latest_started_at,
                latest_completed_status=row.latest_completed_status,
                last_synced_at=row.last_synced_at,
            )
            for row in result
        }
