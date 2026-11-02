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
from app.modules.data_sync.application.data_sync_status_service import (
    DataSourceSyncStatus,
)
from app.modules.data_sync.dependencies import get_data_sync_status_service
from app.modules.data_sync.infrastructure.data_sync_enums import SourceDomain

URL = "/api/v1/data-sync-runs/latest"
COMPANY_ID = uuid.uuid7()
RUN_ID = uuid.uuid7()
STARTED_AT = datetime(2026, 11, 2, 3, 10, tzinfo=UTC)
FINISHED_AT = datetime(2026, 11, 2, 3, 10, 30, tzinfo=UTC)
SYNCED_AT = datetime(2026, 11, 2, 3, 0, 30, tzinfo=UTC)


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


def _status(source_domain, source_system_name, **overrides) -> DataSourceSyncStatus:
    values = {
        "source_domain": source_domain,
        "source_system_name": source_system_name,
        "sync_interval_minutes": 10,
        "data_sync_run_id": RUN_ID,
        "connection_method": "API",
        "status": "PARTIAL_SUCCESS",
        "started_at": STARTED_AT,
        "finished_at": FINISHED_AT,
        "received_count": 43806,
        "loaded_count": 43800,
        "rejected_count": 6,
        "open_issue_count": 6,
        "is_synced": True,
        "last_synced_at": FINISHED_AT,
    }
    values.update(overrides)
    return DataSourceSyncStatus(**values)


NO_HISTORY = {
    "data_sync_run_id": None,
    "connection_method": None,
    "status": None,
    "started_at": None,
    "finished_at": None,
    "received_count": None,
    "loaded_count": None,
    "rejected_count": None,
    "open_issue_count": 0,
    "is_synced": False,
    "last_synced_at": None,
}


class FakeDataSyncStatusService:
    """서비스는 단위 테스트에서 검증하므로, 라우터 테스트에서는 받은 테넌트만 기록하고 고정 결과를 돌려준다."""

    def __init__(self):
        self.tenants: list = []

    async def get_latest_statuses(self, tenant):
        self.tenants.append(tenant)
        return [
            _status(SourceDomain.ORDER, "SAP ERP"),
            _status(
                SourceDomain.INVENTORY,
                "WMS",
                status="FAILED",
                finished_at=None,
                received_count=None,
                loaded_count=None,
                rejected_count=None,
                open_issue_count=0,
                is_synced=False,
                last_synced_at=SYNCED_AT,
            ),
            _status(SourceDomain.SHIPMENT, "TMS", **NO_HISTORY),
            _status(SourceDomain.PRODUCT, "SAP ERP", **NO_HISTORY),
            _status(SourceDomain.WAREHOUSE, "WMS", **NO_HISTORY),
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
async def test_고객사_사용자는_200과_camelCase_다섯_항목을_받는다(
    fake_service, fake_session, role_code
):
    response = await _get(cookies=_access_cookie(role_code, company_id=COMPANY_ID))

    assert response.status_code == 200
    assert "set-cookie" not in response.headers

    body = response.json()
    assert list(body) == ["items"]
    assert [s["sourceDomain"] for s in body["items"]] == [
        "ORDER",
        "INVENTORY",
        "SHIPMENT",
        "PRODUCT",
        "WAREHOUSE",
    ]
    assert body["items"][0] == {
        "dataSyncRunId": str(RUN_ID),
        "sourceDomain": "ORDER",
        "connectionMethod": "API",
        "status": "PARTIAL_SUCCESS",
        "startedAt": "2026-11-02T03:10:00Z",
        "finishedAt": "2026-11-02T03:10:30Z",
        "receivedCount": 43806,
        "loadedCount": 43800,
        "rejectedCount": 6,
        "openIssueCount": 6,
        "sourceSystemName": "SAP ERP",
        "syncIntervalMinutes": 10,
        "isSynced": True,
        "lastSyncedAt": "2026-11-02T03:10:30Z",
    }


async def test_실패한_실행은_건수가_null_이고_이전_성공_시각을_유지한다(
    fake_service, fake_session
):
    response = await _get(
        cookies=_access_cookie("CLIENT_MEMBER", company_id=COMPANY_ID)
    )

    inventory = response.json()["items"][1]
    assert inventory["status"] == "FAILED"
    assert inventory["finishedAt"] is None
    assert inventory["receivedCount"] is None
    assert inventory["isSynced"] is False
    assert inventory["lastSyncedAt"] == "2026-11-02T03:00:30Z"


async def test_이력_없는_원천은_실행_필드가_null_이고_필드를_빼지_않는다(
    fake_service, fake_session
):
    response = await _get(
        cookies=_access_cookie("CLIENT_MEMBER", company_id=COMPANY_ID)
    )

    product = response.json()["items"][3]
    assert product == {
        "dataSyncRunId": None,
        "sourceDomain": "PRODUCT",
        "connectionMethod": None,
        "status": None,
        "startedAt": None,
        "finishedAt": None,
        "receivedCount": None,
        "loadedCount": None,
        "rejectedCount": None,
        "openIssueCount": 0,
        "sourceSystemName": "SAP ERP",
        "syncIntervalMinutes": 10,
        "isSynced": False,
        "lastSyncedAt": None,
    }


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
