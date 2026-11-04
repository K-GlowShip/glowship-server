import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import IntEnum

from app.core.security.tenant import TenantContext
from app.modules.dashboard.infrastructure.repository.dashboard_repository import (
    DashboardRepository,
)

"""
    대시보드 조회 서비스 (API 명세 v6.2 4.4, 홈 대시보드 SCR-002)
      - 고객사 범위는 토큰의 company_id(TenantContext)다. RLS 컨텍스트는 get_tenant_context 가 설정해 둔다.
      - 기준 시각 T = 고객사의 마지막 수집 완료 시각, 기준일 = T 의 UTC 날짜 (명세 C22).
        2026 H1 데이터처럼 수집이 멈춘 데이터에서도 "데이터의 지금" 기준으로 보이게 하려는 것이다.
      - 입고 추이 기간은 [기준일 − days + 1일, 기준일] 이고, 0건인 날도 0 으로 채워 날짜마다 점 하나를 돌려준다.
        적재된 수집이 없으면 기준일이 없으므로 points 는 [] 다.
"""


class InboundTrendDays(IntEnum):
    """입고 추이 기간 (명세 4.4: 7·30·90일, 기본 7)"""

    WEEK = 7
    MONTH = 30
    QUARTER = 90


@dataclass(frozen=True)
class InboundTrendPoint:
    day: date
    planned_count: int
    actual_count: int


@dataclass(frozen=True)
class InboundTrendResult:
    points: list[InboundTrendPoint]


def basis_date_of(basis_at: datetime) -> date:
    """기준 시각 T 의 UTC 날짜 (명세 C22). DB 는 timestamptz 를 돌려주므로 UTC 로 맞춘 뒤 날짜를 뗀다."""
    return basis_at.astimezone(UTC).date()


class DashboardQueryService:
    def __init__(self, repo: DashboardRepository):
        self.repo = repo

    async def get_inbound_trend(
        self,
        tenant: TenantContext,
        *,
        days: InboundTrendDays = InboundTrendDays.WEEK,
        warehouse_id: uuid.UUID | None = None,
    ) -> InboundTrendResult:
        basis_at = await self.repo.find_basis_at(tenant.company_id)
        if basis_at is None:
            return InboundTrendResult(points=[])
        end_date = basis_date_of(basis_at)
        start_date = end_date - timedelta(days=int(days) - 1)
        counts = {
            count.day: count
            for count in await self.repo.find_inbound_counts(
                tenant.company_id,
                start_date=start_date,
                end_date=end_date,
                warehouse_id=warehouse_id,
            )
        }
        points = []
        for offset in range(int(days)):
            day = start_date + timedelta(days=offset)
            count = counts.get(day)
            points.append(
                InboundTrendPoint(
                    day=day,
                    planned_count=count.planned_count if count else 0,
                    actual_count=count.actual_count if count else 0,
                )
            )
        return InboundTrendResult(points=points)
