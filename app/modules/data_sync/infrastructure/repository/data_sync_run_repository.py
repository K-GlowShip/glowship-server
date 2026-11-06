import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ARRAY, UUID, Text, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

"""
    외부 데이터 수집 실행 이력 조회
      - 연동 데이터 조회 화면은 원천 유형별로 "가장 최근 실행", "마지막으로 끝난 실행", "마지막으로 적재된 실행"
        세 가지를 다른 용도로 쓴다. 한 번의 SQL 로 세 가지와 미해결 이슈 수를 원천별 한 행에 모은다.
          latest      : 상태와 무관한 가장 최근 실행 → 명세 4.3 의 "마지막 수집 1건" (실행 필드 전부),
                        started_at 은 수집이 멈췄는지 판정에도 쓴다
          completed   : RUNNING 을 뺀 가장 최근 실행 → status (동기화 성공 여부 판정)
          applied     : SUCCEEDED·PARTIAL_SUCCESS 중 applied_seq 최대 → finished_at (lastSyncedAt)
          open_issues : 원천의 모든 실행에 걸린 OPEN 검증 이슈 수 (탭 배지 "상품 2")
      - 운영에서는 RLS 가 고객사 범위를 강제하지만, 조건을 명시해 로컬(superuser)에서도 같은 결과가 나오게 한다.
"""

_LATEST_BY_DOMAIN = text(
    """
    WITH latest AS (
        SELECT DISTINCT ON (source_domain)
               source_domain, data_sync_run_id, connection_method, status, started_at, finished_at,
               received_record_count, loaded_record_count, rejected_record_count
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
    ),
    open_issues AS (
        SELECT r.source_domain, COUNT(*) AS open_issue_count
          FROM data_validation_issues i
          JOIN data_sync_runs r
            ON r.company_id = i.company_id AND r.data_sync_run_id = i.data_sync_run_id
         WHERE i.company_id = :company_id AND i.status = 'OPEN'
           AND r.source_domain = ANY(:source_domains)
         GROUP BY r.source_domain
    )
    SELECT l.source_domain,
           l.data_sync_run_id,
           l.connection_method,
           l.status,
           l.started_at,
           l.finished_at,
           l.received_record_count,
           l.loaded_record_count,
           l.rejected_record_count,
           c.status AS latest_completed_status,
           a.finished_at AS last_synced_at,
           COALESCE(o.open_issue_count, 0) AS open_issue_count
      FROM latest l
      LEFT JOIN completed c ON c.source_domain = l.source_domain
      LEFT JOIN applied a ON a.source_domain = l.source_domain
      LEFT JOIN open_issues o ON o.source_domain = l.source_domain
    """
).bindparams(
    bindparam("company_id", type_=UUID(as_uuid=True)),
    bindparam("source_domains", type_=ARRAY(Text())),
)


# 원천의 마지막 적재 수집 시작 시각 (적용 순번 최대, C32 와 같은 기준). 가상 시각 계산에 쓴다.
_LAST_APPLIED_STARTED_AT = text(
    """
    SELECT started_at
      FROM data_sync_runs
     WHERE company_id = :company_id AND source_domain = :source_domain
       AND status IN ('SUCCEEDED', 'PARTIAL_SUCCESS')
     ORDER BY applied_seq DESC
     LIMIT 1
    """
).bindparams(bindparam("company_id", type_=UUID(as_uuid=True)))


_LATEST_STARTED_AT = text(
    "SELECT max(started_at) FROM data_sync_runs WHERE company_id = :company_id"
).bindparams(bindparam("company_id", type_=UUID(as_uuid=True)))

# 고객사의 마지막 적재 수집 시작(원천 무관, 초 단위). 가상 시각의 분석 실행 시각(이슈 #50)
_LAST_APPLIED_STARTED_AT_OF_COMPANY = text(
    """
    SELECT date_trunc('second', max(started_at))
      FROM data_sync_runs
     WHERE company_id = :company_id AND applied_seq IS NOT NULL
    """
).bindparams(bindparam("company_id", type_=UUID(as_uuid=True)))

_COMPANY_CODE = text(
    "SELECT company_code FROM companies WHERE company_id = :company_id"
).bindparams(bindparam("company_id", type_=UUID(as_uuid=True)))


@dataclass(frozen=True)
class LatestRunSummary:
    """
    원천 하나의 최신 수집 요약. 수집 이력이 한 건이라도 있어야 만들어진다.
      - data_sync_run_id ~ rejected_count: 가장 최근 실행(RUNNING 포함). 건수는 RUNNING·FAILED 면 None 일 수 있다
      - latest_completed_status: RUNNING 을 뺀 가장 최근 실행의 상태. 끝난 실행이 없으면 None
      - last_synced_at: 마지막으로 적재된 실행의 종료 시각. 성공한 적이 없으면 None
      - open_issue_count: 이 원천의 실행에 걸린 OPEN 검증 이슈 수
    """

    data_sync_run_id: uuid.UUID
    connection_method: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    received_count: int | None
    loaded_count: int | None
    rejected_count: int | None
    latest_completed_status: str | None
    last_synced_at: datetime | None
    open_issue_count: int


class DataSyncRunRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # Read
    async def find_last_applied_started_at(
        self, company_id: uuid.UUID, source_domain: str
    ) -> datetime | None:
        """원천의 마지막 적재 수집(SUCCEEDED·PARTIAL_SUCCESS 중 applied_seq 최대) 시작 시각. 없으면 None"""
        return await self.db.scalar(
            _LAST_APPLIED_STARTED_AT,
            {"company_id": company_id, "source_domain": source_domain},
        )

    async def find_latest_started_at(self, company_id: uuid.UUID) -> datetime | None:
        """고객사의 가장 최근 수집 시작 시각(원천·상태 무관). 수집 이력이 없으면 None. 판단 시각(decision)에 씀"""
        return await self.db.scalar(_LATEST_STARTED_AT, {"company_id": company_id})

    async def find_last_applied_started_at_of_company(
        self, company_id: uuid.UUID
    ) -> datetime | None:
        """고객사의 마지막 적재 수집(적용 순번 있음, 원천 무관) 시작 시각(초 단위). 없으면 None. 분석 실행 시각(이슈 #50)에 씀"""
        return await self.db.scalar(
            _LAST_APPLIED_STARTED_AT_OF_COMPANY, {"company_id": company_id}
        )

    async def find_company_code(self, company_id: uuid.UUID) -> str | None:
        """고객사 코드 (가상 TMS 시나리오의 _demo_company 와 맞춤)"""
        return await self.db.scalar(_COMPANY_CODE, {"company_id": company_id})

    async def find_latest_by_domain(
        self, company_id: uuid.UUID, source_domains: Iterable[str]
    ) -> dict[str, LatestRunSummary]:
        """고객사의 원천별 최신 수집 요약. 수집 이력이 없는 원천은 결과에 없다."""
        result = await self.db.execute(
            _LATEST_BY_DOMAIN,
            {"company_id": company_id, "source_domains": list(source_domains)},
        )
        return {
            row.source_domain: LatestRunSummary(
                data_sync_run_id=row.data_sync_run_id,
                connection_method=row.connection_method,
                status=row.status,
                started_at=row.started_at,
                finished_at=row.finished_at,
                received_count=row.received_record_count,
                loaded_count=row.loaded_record_count,
                rejected_count=row.rejected_record_count,
                latest_completed_status=row.latest_completed_status,
                last_synced_at=row.last_synced_at,
                open_issue_count=row.open_issue_count,
            )
            for row in result
        }
