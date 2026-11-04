import uuid
from dataclasses import dataclass, field
from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security.cookies import ACCESS_TOKEN_COOKIE
from app.core.security.jwt import create_access_token
from app.main import app
from app.modules.dashboard.application.dashboard_query_service import (
    InboundTrendDays,
    InboundTrendPoint,
    InboundTrendResult,
)
from app.modules.dashboard.dependencies import get_dashboard_query_service

INBOUND_TREND_URL = "/api/v1/dashboard/inbound-trend"
COMPANY_ID = uuid.uuid7()


def _access_cookie(role_code: str, company_id: uuid.UUID | None = None) -> dict:
    token = create_access_token(
        user_id=uuid.uuid7(),
        role_code=role_code,
        company_id=company_id,
        config=get_settings().jwt_config,
    )
    return {ACCESS_TOKEN_COOKIE: token}


async def _get(url: str, cookies: dict | None = None, params: dict | None = None):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", cookies=cookies
    ) as client:
        return await client.get(url, params=params)


@dataclass
class FakeSession:
    """DB 없이 실행한다. get_tenant_context 가 RLS 컨텍스트 SQL 을 실제로 실행하는지 기록한다."""

    executed: list = field(default_factory=list)

    async def execute(self, statement, params=None):
        self.executed.append((str(statement), params))


class FakeDashboardQueryService:
    """서비스는 단위 테스트에서 검증하므로, 라우터 테스트에서는 받은 인자만 기록하고 고정 결과를 돌려준다."""

    def __init__(self):
        self.calls: list = []

    async def get_inbound_trend(self, tenant, **kwargs):
        self.calls.append((tenant, kwargs))
        return InboundTrendResult(
            points=[
                InboundTrendPoint(
                    day=date(2026, 6, 30), planned_count=2, actual_count=1
                )
            ]
        )


@pytest.fixture
def fake_session():
    session = FakeSession()

    async def _get_db():
        yield session

    app.dependency_overrides[get_db] = _get_db
    yield session
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def fake_service(fake_session):
    service = FakeDashboardQueryService()
    app.dependency_overrides[get_dashboard_query_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_dashboard_query_service, None)


async def test_입고_추이_쿠키가_없으면_401을_반환한다(fake_service, fake_session):
    response = await _get(INBOUND_TREND_URL)

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHENTICATED"
    assert fake_service.calls == []


@pytest.mark.parametrize("role_code", ["OWNER", "ADMIN"])
async def test_입고_추이_고객사_범위가_없는_역할은_403을_반환한다(
    fake_service, fake_session, role_code
):
    response = await _get(INBOUND_TREND_URL, cookies=_access_cookie(role_code))

    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
    assert fake_service.calls == []


@pytest.mark.parametrize(
    "params",
    [
        {"days": "14"},
        {"days": "0"},
        {"days": "7.0"},
        {"days": "07"},
        {"days": "+7"},
        {"days": " 7"},
        {"warehouseId": "not-a-uuid"},
    ],
)
async def test_입고_추이_쿼리_파라미터가_형식에_어긋나면_422를_반환한다(
    fake_service, fake_session, params
):
    response = await _get(
        INBOUND_TREND_URL,
        cookies=_access_cookie("CLIENT_MEMBER", company_id=COMPANY_ID),
        params=params,
    )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert fake_service.calls == []


@pytest.mark.parametrize("role_code", ["CLIENT_MEMBER", "CLIENT_LEAD"])
async def test_입고_추이는_200과_camelCase_점_목록을_돌려준다(
    fake_service, fake_session, role_code
):
    response = await _get(
        INBOUND_TREND_URL, cookies=_access_cookie(role_code, company_id=COMPANY_ID)
    )

    assert response.status_code == 200
    assert response.json() == {
        "points": [{"date": "2026-06-30", "plannedCount": 2, "actualCount": 1}]
    }
    tenant, kwargs = fake_service.calls[0]
    assert tenant.company_id == COMPANY_ID
    assert kwargs == {"days": InboundTrendDays.WEEK, "warehouse_id": None}
    assert fake_session.executed, "RLS 컨텍스트 SQL 이 실행되어야 한다"


@pytest.mark.parametrize("days", ["7", "30", "90"])
async def test_입고_추이는_days_와_창고를_서비스에_넘긴다(
    fake_service, fake_session, days
):
    warehouse_id = uuid.uuid7()

    await _get(
        INBOUND_TREND_URL,
        cookies=_access_cookie("CLIENT_LEAD", company_id=COMPANY_ID),
        params={"days": days, "warehouseId": str(warehouse_id)},
    )

    _, kwargs = fake_service.calls[0]
    assert kwargs == {"days": InboundTrendDays(int(days)), "warehouse_id": warehouse_id}
