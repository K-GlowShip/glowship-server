import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security.cookies import ACCESS_TOKEN_COOKIE
from app.core.security.jwt import create_access_token
from app.main import app
from app.modules.dashboard.application.dashboard_query_service import (
    DashboardSummary,
)
from app.modules.dashboard.dependencies import get_dashboard_query_service

URL = "/api/v1/dashboard/summary"
COMPANY_ID = uuid.uuid7()


def _access_cookie(role_code: str, company_id: uuid.UUID | None = None) -> dict:
    token = create_access_token(
        user_id=uuid.uuid7(),
        role_code=role_code,
        company_id=company_id,
        config=get_settings().jwt_config,
    )
    return {ACCESS_TOKEN_COOKIE: token}


async def _get(cookies: dict | None = None):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", cookies=cookies
    ) as client:
        return await client.get(URL)


@dataclass
class FakeSession:
    executed: list = field(default_factory=list)

    async def execute(self, statement, params=None):
        self.executed.append((str(statement), params))


class FakeDashboardQueryService:
    def __init__(self, summary: DashboardSummary):
        self.summary = summary
        self.calls: list = []

    async def get_summary(self, tenant):
        self.calls.append(tenant)
        return self.summary


SUMMARY = DashboardSummary(
    last_synced_at=datetime(2026, 7, 1, 0, 4, 30, tzinfo=UTC),
    in_transit_shipment_count=5,
    week_over_week=1,
    delayed_shipment_count=2,
    day_over_day=-1,
    on_time_inbound_rate=0.75,
    month_over_month=-0.25,
    at_risk_order_count=3,
    at_risk_order_rate=0.0173,
)
EMPTY = DashboardSummary(
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


@pytest.fixture
def fake_session():
    session = FakeSession()

    async def _get_db():
        yield session

    app.dependency_overrides[get_db] = _get_db
    yield session
    app.dependency_overrides.pop(get_db, None)


def _use(summary: DashboardSummary) -> FakeDashboardQueryService:
    service = FakeDashboardQueryService(summary)
    app.dependency_overrides[get_dashboard_query_service] = lambda: service
    return service


@pytest.fixture(autouse=True)
def _clear_service_override():
    yield
    app.dependency_overrides.pop(get_dashboard_query_service, None)


async def test_요약_쿠키가_없으면_401(fake_session):
    service = _use(SUMMARY)

    response = await _get()

    assert response.status_code == 401
    assert service.calls == []


@pytest.mark.parametrize("role_code", ["OWNER", "ADMIN"])
async def test_요약_고객사_범위가_없는_역할은_403(fake_session, role_code):
    service = _use(SUMMARY)

    response = await _get(cookies=_access_cookie(role_code))

    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
    assert service.calls == []


@pytest.mark.parametrize("role_code", ["CLIENT_MEMBER", "CLIENT_LEAD"])
async def test_요약은_KPI_4개와_비교값을_camelCase_로_돌려준다(fake_session, role_code):
    service = _use(SUMMARY)

    response = await _get(cookies=_access_cookie(role_code, company_id=COMPANY_ID))

    assert response.status_code == 200
    assert response.json() == {
        "lastSyncedAt": "2026-07-01T00:04:30Z",
        "inTransitShipmentCount": 5,
        "weekOverWeek": 1,
        "delayedShipmentCount": 2,
        "dayOverDay": -1,
        "onTimeInboundRate": 0.75,
        "monthOverMonth": -0.25,
        "atRiskOrderCount": 3,
        "atRiskOrderRate": 0.0173,
    }
    assert service.calls[0].company_id == COMPANY_ID
    assert fake_session.executed, "RLS 컨텍스트 SQL 이 실행되어야 한다"


async def test_적재된_수집이_없으면_null_과_0을_돌려준다(fake_session):
    _use(EMPTY)

    response = await _get(
        cookies=_access_cookie("CLIENT_MEMBER", company_id=COMPANY_ID)
    )

    assert response.json() == {
        "lastSyncedAt": None,
        "inTransitShipmentCount": 0,
        "weekOverWeek": None,
        "delayedShipmentCount": 0,
        "dayOverDay": None,
        "onTimeInboundRate": None,
        "monthOverMonth": None,
        "atRiskOrderCount": 0,
        "atRiskOrderRate": None,
    }
