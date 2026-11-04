import uuid
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import UUID, Date, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

"""
    대시보드 집계 조회 (API 명세 v6.2 4.4 GET /dashboard/summary·/inbound-trend, 홈 대시보드 SCR-002)
      - 대시보드 값은 조회할 때 계산하고 저장하지 않는다.
      - 기준 시각 T 는 고객사의 마지막 수집 완료 시각이다 (명세 C22). 서버의 지금 시각이 아니다.
        적재된 수집(SUCCEEDED·PARTIAL_SUCCESS)의 finished_at 최댓값이고, GET /data-sync-runs/latest 의 lastSyncedAt 과 같은 기준이다.
      - 입고 추이는 날짜별 선적 수다: 계획 = planned_3pl_inbound_date, 실제 = actual_3pl_inbound_date.
        0건인 날은 여기서 돌려주지 않고 서비스가 채운다.
      - 운영에서는 RLS 가 고객사 범위를 강제하지만, 로컬 개발 DB 는 슈퍼유저라 RLS 를 우회하므로
        조건을 명시해 같은 결과가 나오게 한다 (ERD 설명 4.6).
"""

_UUID = UUID(as_uuid=True)

_BASIS_AT = text(
    """
    SELECT max(finished_at)
      FROM data_sync_runs
     WHERE company_id = :company_id
       AND status IN ('SUCCEEDED', 'PARTIAL_SUCCESS')
    """
).bindparams(bindparam("company_id", type_=_UUID))

_INBOUND_COUNTS = text(
    """
    WITH scoped AS (
        SELECT planned_3pl_inbound_date, actual_3pl_inbound_date
          FROM shipments
         WHERE company_id = :company_id
           AND (CAST(:warehouse_id AS uuid) IS NULL OR warehouse_id = :warehouse_id)
    ),
    marks AS (
        SELECT planned_3pl_inbound_date AS day, 1 AS planned, 0 AS actual
          FROM scoped
         WHERE planned_3pl_inbound_date BETWEEN :start_date AND :end_date
        UNION ALL
        SELECT actual_3pl_inbound_date, 0, 1
          FROM scoped
         WHERE actual_3pl_inbound_date BETWEEN :start_date AND :end_date
    )
    SELECT day, SUM(planned) AS planned_count, SUM(actual) AS actual_count
      FROM marks
     GROUP BY day
     ORDER BY day
    """
).bindparams(
    bindparam("company_id", type_=_UUID),
    bindparam("warehouse_id", type_=_UUID),
    bindparam("start_date", type_=Date()),
    bindparam("end_date", type_=Date()),
)


@dataclass(frozen=True)
class InboundCount:
    """하루의 입고 예정·실제 입고 선적 수. 0건인 날은 행이 없다."""

    day: date
    planned_count: int
    actual_count: int


class DashboardRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # Read
    async def find_basis_at(self, company_id: uuid.UUID) -> datetime | None:
        """기준 시각 T: 적재된 수집의 마지막 완료 시각. 적재된 수집이 없으면 None."""
        return await self.db.scalar(_BASIS_AT, {"company_id": company_id})

    async def find_inbound_counts(
        self,
        company_id: uuid.UUID,
        *,
        start_date: date,
        end_date: date,
        warehouse_id: uuid.UUID | None = None,
    ) -> list[InboundCount]:
        """[start_date, end_date] 안의 날짜별 입고 예정·실제 입고 선적 수 (날짜 순, 0건인 날 제외)."""
        rows = await self.db.execute(
            _INBOUND_COUNTS,
            {
                "company_id": company_id,
                "warehouse_id": warehouse_id,
                "start_date": start_date,
                "end_date": end_date,
            },
        )
        return [
            InboundCount(
                day=row.day,
                planned_count=int(row.planned_count),
                actual_count=int(row.actual_count),
            )
            for row in rows
        ]
