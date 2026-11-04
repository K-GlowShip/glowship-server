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
      - 요약 KPI (명세 4.4, C22): 정시 입고율은 (기준일 − 30일, 기준일] 에 입고된 선적 기준이고 0건이면 None,
        위험 주문은 항상 정수(대상 실행이 없으면 0), 위험 주문 비율은 미출고 주문이 0건이면 None.
        적재된 수집이 없으면 기준일이 없으므로 건수는 0, 비율은 None 이고 DB 를 더 읽지 않는다.
      - 비교값 (C22): 진행 중인 선적은 T − 7일 대비 증감(weekOverWeek), 지연 선적은 T − 1일 대비 증감(dayOverDay),
        정시 입고율은 직전 30일 (기준일 − 60일, 기준일 − 30일] 대비 차이(monthOverMonth, 0~1 단위, 어느 쪽이든 None 이면 None).
        적재된 수집이 없으면 비교값은 모두 None.
"""

ON_TIME_WINDOW_DAYS = 30
IN_TRANSIT_COMPARE = timedelta(days=7)
DELAYED_COMPARE = timedelta(days=1)


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


@dataclass(frozen=True)
class DashboardSummary:
    """대시보드 KPI 4개, 각 비교값, 기준 시각 (명세 4.4)"""

    last_synced_at: datetime | None
    in_transit_shipment_count: int
    week_over_week: int | None
    delayed_shipment_count: int
    day_over_day: int | None
    on_time_inbound_rate: float | None
    month_over_month: float | None
    at_risk_order_count: int
    at_risk_order_rate: float | None


def ratio(numerator: int, denominator: int) -> float | None:
    """0~1 비율. 분모가 0 이면 None (응답 규칙: 비율은 0~1, 0건이면 null)"""
    return numerator / denominator if denominator else None


def rate_change(current: float | None, previous: float | None) -> float | None:
    """비율 차이 (0~1 단위). 어느 쪽이든 None 이면 None"""
    if current is None or previous is None:
        return None
    return current - previous


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

    async def get_summary(self, tenant: TenantContext) -> DashboardSummary:
        company_id = tenant.company_id
        basis_at = await self.repo.find_basis_at(company_id)
        if basis_at is None:
            return DashboardSummary(
                last_synced_at=None,
                in_transit_shipment_count=0,
                week_over_week=None,
                delayed_shipment_count=0,
                day_over_day=None,
                on_time_inbound_rate=None,
                month_over_month=None,
                at_risk_order_count=0,
                at_risk_order_rate=None,
            )
        basis_date = basis_date_of(basis_at)
        window = timedelta(days=ON_TIME_WINDOW_DAYS)
        current = await self.repo.find_inbound_timeliness(
            company_id, after_date=basis_date - window, until_date=basis_date
        )
        previous = await self.repo.find_inbound_timeliness(
            company_id,
            after_date=basis_date - 2 * window,
            until_date=basis_date - window,
        )
        on_time_inbound_rate = ratio(current.on_time_count, current.received_count)
        in_transit = await self.repo.count_in_transit_shipments(company_id)
        in_transit_before = await self.repo.count_in_transit_shipments_at(
            company_id, basis_at - IN_TRANSIT_COMPARE
        )
        delayed = await self.repo.count_delayed_shipments(company_id)
        delayed_before = await self.repo.count_delayed_shipments_at(
            company_id, basis_at - DELAYED_COMPARE
        )
        at_risk_order_count = await self.repo.count_at_risk_orders(company_id)
        unshipped_order_count = await self.repo.count_unshipped_orders(company_id)
        return DashboardSummary(
            last_synced_at=basis_at,
            in_transit_shipment_count=in_transit,
            week_over_week=in_transit - in_transit_before,
            delayed_shipment_count=delayed,
            day_over_day=delayed - delayed_before,
            on_time_inbound_rate=on_time_inbound_rate,
            month_over_month=rate_change(
                on_time_inbound_rate,
                ratio(previous.on_time_count, previous.received_count),
            ),
            at_risk_order_count=at_risk_order_count,
            at_risk_order_rate=ratio(at_risk_order_count, unshipped_order_count),
        )
