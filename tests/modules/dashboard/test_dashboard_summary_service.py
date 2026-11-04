import uuid
from datetime import UTC, date, datetime, timedelta

from app.core.security.tenant import TenantContext
from app.modules.dashboard.application.dashboard_query_service import (
    DashboardQueryService,
    DashboardSummary,
    rate_change,
    ratio,
)
from app.modules.dashboard.infrastructure.repository.dashboard_repository import (
    InboundTimeliness,
)

"""
    대시보드 요약 KPI 서비스 단위 테스트 (DB 없음, 명세 4.4·C22)
      - 정시 입고율 기간은 (기준일 − 30일, 기준일], 입고 0건이면 None
      - 위험 주문은 항상 정수, 비율은 미출고 주문이 0건이면 None
      - 적재된 수집이 없으면 건수 0·비율 None 이고 집계를 읽지 않는다
"""

TENANT = TenantContext(user_id=uuid.uuid7(), company_id=uuid.uuid7())
BASIS_AT = datetime(2026, 7, 1, 0, 4, 30, tzinfo=UTC)
TIMELINESS = InboundTimeliness(received_count=4, on_time_count=3)
PREVIOUS_TIMELINESS = InboundTimeliness(received_count=5, on_time_count=5)


class FakeSummaryRepository:
    def __init__(
        self,
        basis_at=BASIS_AT,
        timeliness=TIMELINESS,
        previous_timeliness=PREVIOUS_TIMELINESS,
        at_risk=3,
        unshipped=120,
        in_transit_before=7,
        delayed_before=1,
    ):
        self.basis_at = basis_at
        self.timeliness = timeliness
        self.previous_timeliness = previous_timeliness
        self.in_transit_before = in_transit_before
        self.delayed_before = delayed_before
        self.at_calls: list = []
        self.at_risk = at_risk
        self.unshipped = unshipped
        self.timeliness_calls: list = []
        self.count_calls: list = []

    async def find_basis_at(self, company_id):
        return self.basis_at

    async def find_inbound_timeliness(self, company_id, **kwargs):
        self.timeliness_calls.append(kwargs)
        return (
            self.timeliness
            if len(self.timeliness_calls) == 1
            else self.previous_timeliness
        )

    async def count_in_transit_shipments_at(self, company_id, at):
        self.at_calls.append(("in_transit", at))
        return self.in_transit_before

    async def count_delayed_shipments_at(self, company_id, at):
        self.at_calls.append(("delayed", at))
        return self.delayed_before

    async def count_in_transit_shipments(self, company_id):
        self.count_calls.append("in_transit")
        return 5

    async def count_delayed_shipments(self, company_id):
        self.count_calls.append("delayed")
        return 2

    async def count_at_risk_orders(self, company_id):
        self.count_calls.append("at_risk")
        return self.at_risk

    async def count_unshipped_orders(self, company_id):
        self.count_calls.append("unshipped")
        return self.unshipped


async def test_요약은_KPI_4개와_비교값과_기준_시각을_돌려준다():
    repo = FakeSummaryRepository()

    summary = await DashboardQueryService(repo).get_summary(TENANT)

    assert summary == DashboardSummary(
        last_synced_at=BASIS_AT,
        in_transit_shipment_count=5,
        week_over_week=5 - 7,
        delayed_shipment_count=2,
        day_over_day=2 - 1,
        on_time_inbound_rate=0.75,
        month_over_month=0.75 - 1.0,
        at_risk_order_count=3,
        at_risk_order_rate=3 / 120,
    )
    assert repo.timeliness_calls == [
        {"after_date": date(2026, 6, 1), "until_date": date(2026, 7, 1)},
        {"after_date": date(2026, 5, 2), "until_date": date(2026, 6, 1)},
    ]
    assert repo.at_calls == [
        ("in_transit", BASIS_AT - timedelta(days=7)),
        ("delayed", BASIS_AT - timedelta(days=1)),
    ]


async def test_직전_30일_입고가_0건이면_monthOverMonth_는_None():
    repo = FakeSummaryRepository(
        previous_timeliness=InboundTimeliness(received_count=0, on_time_count=0)
    )

    summary = await DashboardQueryService(repo).get_summary(TENANT)

    assert (summary.on_time_inbound_rate, summary.month_over_month) == (0.75, None)


async def test_입고_0건이면_정시_입고율은_None():
    repo = FakeSummaryRepository(
        timeliness=InboundTimeliness(received_count=0, on_time_count=0)
    )

    summary = await DashboardQueryService(repo).get_summary(TENANT)

    assert summary.on_time_inbound_rate is None
    assert summary.month_over_month is None


async def test_미출고_주문이_0건이면_위험_주문_비율은_None_이고_건수는_정수():
    repo = FakeSummaryRepository(at_risk=0, unshipped=0)

    summary = await DashboardQueryService(repo).get_summary(TENANT)

    assert (summary.at_risk_order_count, summary.at_risk_order_rate) == (0, None)


async def test_위험_주문이_0건이어도_미출고_주문이_있으면_비율은_0():
    repo = FakeSummaryRepository(at_risk=0, unshipped=173)

    summary = await DashboardQueryService(repo).get_summary(TENANT)

    assert summary.at_risk_order_rate == 0.0


async def test_적재된_수집이_없으면_건수_0_비율_None_이고_집계를_읽지_않는다():
    repo = FakeSummaryRepository(basis_at=None)

    summary = await DashboardQueryService(repo).get_summary(TENANT)

    assert summary == DashboardSummary(
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
    assert repo.timeliness_calls == [] and repo.count_calls == []
    assert repo.at_calls == []


def test_비율은_분모가_0이면_None():
    assert ratio(3, 4) == 0.75
    assert ratio(0, 5) == 0.0
    assert ratio(0, 0) is None


def test_비율_차이는_어느_쪽이든_None_이면_None():
    assert rate_change(0.75, 1.0) == -0.25
    assert rate_change(None, 1.0) is None
    assert rate_change(0.75, None) is None
