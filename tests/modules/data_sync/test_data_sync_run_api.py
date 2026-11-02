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
from app.modules.data_sync.application.data_source_catalog import ExternalSystem
from app.modules.data_sync.application.data_sync_status_service import (
    ExternalSystemSyncStatus,
)
from app.modules.data_sync.dependencies import get_data_sync_status_service
from app.modules.data_sync.infrastructure.data_sync_enums import SourceDomain

URL = "/api/v1/data-sync-runs/latest"
COMPANY_ID = uuid.uuid7()
SYNCED_AT = datetime(2026, 11, 2, 3, 15, tzinfo=UTC)


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
    """DB 없이 실행한다. get_tenant_context 가 RLS 컨텍스트 SQL 을 실제로 실행하는지 기록한다."""

    executed: list = field(default_factory=list)

    async def execute(self, statement, params=None):
        self.executed.append((str(statement), params))


class FakeDataSyncStatusService:
    """서비스는 단위 테스트에서 검증하므로, 라우터 테스트에서는 받은 테넌트만 기록하고 고정 결과를 돌려준다."""

    def __init__(self):
        self.tenants: list = []

    async def get_latest_statuses(self, tenant):
        self.tenants.append(tenant)
        return [
            ExternalSystemSyncStatus(
                source_domain=SourceDomain.ORDER,
                system_name=ExternalSystem.ERP,
                display_name="주문 데이터",
                is_synced=True,
                last_synced_at=SYNCED_AT,
                update_interval_seconds=600,
            ),
            ExternalSystemSyncStatus(
                source_domain=SourceDomain.INVENTORY,
                system_name=ExternalSystem.WMS,
                display_name="재고 데이터",
                is_synced=False,
                last_synced_at=SYNCED_AT,
                update_interval_seconds=600,
            ),
            ExternalSystemSyncStatus(
                source_domain=SourceDomain.SHIPMENT,
                system_name=ExternalSystem.TMS,
                display_name="선적 데이터",
                is_synced=False,
                last_synced_at=None,
                update_interval_seconds=600,
            ),
        ]


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
    service = FakeDataSyncStatusService()
    app.dependency_overrides[get_data_sync_status_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_data_sync_status_service, None)


async def test_쿠키가_없으면_401을_반환한다(fake_service, fake_session):
    response = await _get()

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHENTICATED"
    assert fake_service.tenants == []
    assert fake_session.executed == []


@pytest.mark.parametrize("role_code", ["OWNER", "ADMIN"])
async def test_고객사_범위가_없는_역할은_403을_반환한다(
    fake_service, fake_session, role_code
):
    response = await _get(cookies=_access_cookie(role_code))

    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
    assert fake_service.tenants == []
    assert fake_session.executed == []


@pytest.mark.parametrize("role_code", ["CLIENT_MEMBER", "CLIENT_LEAD"])
async def test_고객사_사용자는_200과_camelCase_세_항목을_받는다(
    fake_service, fake_session, role_code
):
    response = await _get(cookies=_access_cookie(role_code, company_id=COMPANY_ID))

    assert response.status_code == 200
    assert "set-cookie" not in response.headers

    body = response.json()
    assert list(body) == ["externalSystems"]
    assert [s["sourceDomain"] for s in body["externalSystems"]] == [
        "ORDER",
        "INVENTORY",
        "SHIPMENT",
    ]
    assert body["externalSystems"][0] == {
        "sourceDomain": "ORDER",
        "systemName": "ERP",
        "displayName": "주문 데이터",
        "isSynced": True,
        "lastSyncedAt": "2026-11-02T03:15:00Z",
        "updateIntervalSeconds": 600,
    }
    assert body["externalSystems"][2]["lastSyncedAt"] is None


async def test_토큰의_고객사로_RLS_컨텍스트를_설정하고_서비스에_넘긴다(
    fake_service, fake_session
):
    await _get(cookies=_access_cookie("CLIENT_MEMBER", company_id=COMPANY_ID))

    [tenant] = fake_service.tenants
    assert tenant.company_id == COMPANY_ID

    [(sql, params)] = fake_session.executed
    assert "set_config('app.company_id'" in sql
    assert params["company_id"] == str(COMPANY_ID)
    assert params["user_id"] == str(tenant.user_id)
