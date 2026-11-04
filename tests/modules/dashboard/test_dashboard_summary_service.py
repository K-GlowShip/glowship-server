import uuid
from datetime import UTC, date, datetime

from app.core.security.tenant import TenantContext
from app.modules.dashboard.application.dashboard_query_service import (
    DashboardQueryService,
    DashboardSummary,
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


class FakeSummaryRepository:
    def __init__(
        self,
        basis_at=BASIS_AT,
        timeliness=TIMELINESS,
        at_risk=3,
        unshipped=120,
    ):
        self.basis_at = basis_at
        self.timeliness = timeliness
        self.at_risk = at_risk
        self.unshipped = unshipped
        self.timeliness_calls: list = []
        self.count_calls: list = []

    async def find_basis_at(self, company_id):
        return self.basis_at

    async def find_inbound_timeliness(self, company_id, **kwargs):
        self.timeliness_calls.append(kwargs)
        return self.timeliness

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


async def test_요약은_KPI_4개와_기준_시각을_돌려준다():
    repo = FakeSummaryRepository()

    summary = await DashboardQueryService(repo).get_summary(TENANT)

    assert summary == DashboardSummary(
        last_synced_at=BASIS_AT,
        in_transit_shipment_count=5,
        delayed_shipment_count=2,
        on_time_inbound_rate=0.75,
        at_risk_order_count=3,
        at_risk_order_rate=3 / 120,
    )
    assert repo.timeliness_calls == [
        {"after_date": date(2026, 6, 1), "until_date": date(2026, 7, 1)}
    ]


async def test_입고_0건이면_정시_입고율은_None():
    repo = FakeSummaryRepository(
        timeliness=InboundTimeliness(received_count=0, on_time_count=0)
    )

    summary = await DashboardQueryService(repo).get_summary(TENANT)

    assert summary.on_time_inbound_rate is None


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
        delayed_shipment_count=0,
        on_time_inbound_rate=None,
        at_risk_order_count=0,
        at_risk_order_rate=None,
    )
    assert repo.timeliness_calls == [] and repo.count_calls == []


def test_비율은_분모가_0이면_None():
    assert ratio(3, 4) == 0.75
    assert ratio(0, 5) == 0.0
    assert ratio(0, 0) is None
