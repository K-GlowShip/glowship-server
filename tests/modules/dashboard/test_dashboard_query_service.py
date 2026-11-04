import uuid
from datetime import UTC, date, datetime, timedelta, timezone

from app.core.security.tenant import TenantContext
from app.modules.dashboard.application.dashboard_query_service import (
    DashboardQueryService,
    InboundTrendDays,
    basis_date_of,
)
from app.modules.dashboard.infrastructure.repository.dashboard_repository import (
    InboundCount,
)

"""
    대시보드 조회 서비스 단위 테스트 (DB 없음)
      - 기준일은 마지막 수집 완료 시각의 UTC 날짜이고(명세 C22), 입고 추이는 기준일까지 days 일을 0건인 날까지 채운다.
"""

TENANT = TenantContext(user_id=uuid.uuid7(), company_id=uuid.uuid7())
WAREHOUSE_ID = uuid.uuid7()
BASIS_AT = datetime(2026, 7, 1, 0, 4, 30, tzinfo=UTC)


class FakeDashboardRepository:
    def __init__(self, basis_at=BASIS_AT, counts=()):
        self.basis_at = basis_at
        self.counts = list(counts)
        self.inbound_calls: list = []

    async def find_basis_at(self, company_id):
        return self.basis_at

    async def find_inbound_counts(self, company_id, **kwargs):
        self.inbound_calls.append((company_id, kwargs))
        return self.counts


async def test_입고_추이는_기준일까지_days_일을_0건인_날까지_채운다():
    repo = FakeDashboardRepository(
        counts=[
            InboundCount(day=date(2026, 6, 25), planned_count=0, actual_count=1),
            InboundCount(day=date(2026, 6, 28), planned_count=2, actual_count=0),
        ]
    )

    result = await DashboardQueryService(repo).get_inbound_trend(
        TENANT, days=InboundTrendDays.WEEK, warehouse_id=WAREHOUSE_ID
    )

    assert [(p.day, p.planned_count, p.actual_count) for p in result.points] == [
        (date(2026, 6, 25), 0, 1),
        (date(2026, 6, 26), 0, 0),
        (date(2026, 6, 27), 0, 0),
        (date(2026, 6, 28), 2, 0),
        (date(2026, 6, 29), 0, 0),
        (date(2026, 6, 30), 0, 0),
        (date(2026, 7, 1), 0, 0),
    ]
    assert repo.inbound_calls == [
        (
            TENANT.company_id,
            {
                "start_date": date(2026, 6, 25),
                "end_date": date(2026, 7, 1),
                "warehouse_id": WAREHOUSE_ID,
            },
        )
    ]


async def test_입고_추이_90일은_기준일을_포함해_90개_점이다():
    result = await DashboardQueryService(FakeDashboardRepository()).get_inbound_trend(
        TENANT, days=InboundTrendDays.QUARTER
    )

    assert len(result.points) == 90
    assert result.points[0].day == date(2026, 7, 1) - timedelta(days=89)
    assert result.points[-1].day == date(2026, 7, 1)


async def test_적재된_수집이_없으면_입고_추이는_빈_목록이고_집계를_읽지_않는다():
    repo = FakeDashboardRepository(basis_at=None)

    result = await DashboardQueryService(repo).get_inbound_trend(TENANT)

    assert result.points == []
    assert repo.inbound_calls == []


def test_기준일은_UTC_날짜다():
    """KST 7/1 08:00 = UTC 6/30 23:00 → 기준일은 6/30 (C22: T 의 UTC 날짜)"""
    kst = timezone(timedelta(hours=9))

    assert basis_date_of(datetime(2026, 7, 1, 8, 0, tzinfo=kst)) == date(2026, 6, 30)
    assert basis_date_of(BASIS_AT) == date(2026, 7, 1)
