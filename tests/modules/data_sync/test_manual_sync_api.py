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
from app.modules.data_sync.application.exception.data_sync_exceptions import (
    SourceNotSupportedException,
    SyncInProgressException,
)
from app.modules.data_sync.application.ingest.ingest_service import StartedRun
from app.modules.data_sync.application.manual_sync_service import ManualSyncTicket
from app.modules.data_sync.application.virtual_clock import SyncWindow
from app.modules.data_sync.dependencies import get_manual_sync_service
from app.modules.data_sync.infrastructure.data_sync_enums import SourceDomain

"""
    수동 수집 API 25 라우터 테스트 (POST /data-sync-runs). 서비스는 SQL 시험이 검증하므로
    여기서는 역할, 요청 형식, 202 응답, 백그라운드 실행 등록, 오류 코드 전달을 본다.
"""

COMPANY_ID = uuid.uuid7()
RUN_ID = uuid.uuid7()
URL = "/api/v1/data-sync-runs"
AT = datetime(2026, 7, 2, tzinfo=UTC)


def _cookie(role_code: str) -> dict:
    token = create_access_token(
        user_id=uuid.uuid7(),
        role_code=role_code,
        company_id=None if role_code in ("ADMIN", "OWNER") else COMPANY_ID,
        config=get_settings().jwt_config,
    )
    return {ACCESS_TOKEN_COOKIE: token}


async def _post(body, role: str | None = "CLIENT_MEMBER"):
    cookies = _cookie(role) if role else None
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", cookies=cookies
    ) as client:
        return await client.post(URL, json=body)


@dataclass
class FakeSession:
    executed: list = field(default_factory=list)

    async def execute(self, statement, params=None):
        self.executed.append((str(statement), params))


class FakeService:
    def __init__(self):
        self.requests: list = []
        self.runs: list = []
        self.error: Exception | None = None

    async def request(self, tenant, source_domain):
        self.requests.append((tenant, source_domain))
        if self.error is not None:
            raise self.error
        return ManualSyncTicket(
            started=StartedRun(tenant.company_id, RUN_ID, source_domain, AT),
            window=SyncWindow(after=None, at=AT),
            company_code="A_BEAUTY",
        )

    async def run(self, ticket):
        self.runs.append(ticket)


@pytest.fixture
def fake_service():
    session = FakeSession()

    async def _get_db():
        yield session

    service = FakeService()
    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_manual_sync_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_manual_sync_service, None)


@pytest.mark.parametrize("role", ["CLIENT_MEMBER", "CLIENT_LEAD"])
async def test_client_starts_sync_and_gets_202_running(fake_service, role):
    response = await _post({"sourceDomain": "SHIPMENT"}, role=role)

    assert response.status_code == 202
    assert response.json() == {"dataSyncRunId": str(RUN_ID), "status": "RUNNING"}
    [(tenant, domain)] = fake_service.requests
    assert (tenant.company_id, domain) == (COMPANY_ID, SourceDomain.SHIPMENT)
    # 응답 뒤 백그라운드로 적용을 한 번 실행한다
    [ticket] = fake_service.runs
    assert ticket.started.data_sync_run_id == RUN_ID


async def test_without_cookie_returns_401(fake_service):
    response = await _post({"sourceDomain": "SHIPMENT"}, role=None)

    assert response.status_code == 401
    assert fake_service.requests == []


@pytest.mark.parametrize("role", ["ADMIN", "OWNER"])
async def test_non_client_returns_403(fake_service, role):
    response = await _post({"sourceDomain": "SHIPMENT"}, role=role)

    assert response.status_code == 403
    assert fake_service.requests == []


@pytest.mark.parametrize("body", [{}, {"sourceDomain": "RAIL"}, {"sourceDomain": None}])
async def test_invalid_body_returns_422(fake_service, body):
    response = await _post(body)

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert fake_service.requests == []


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (SyncInProgressException(), 409, "SYNC_IN_PROGRESS"),
        (SourceNotSupportedException(), 422, "VALIDATION_ERROR"),
    ],
)
async def test_service_rejection_is_returned_without_background_run(
    fake_service, error, status_code, code
):
    fake_service.error = error

    response = await _post({"sourceDomain": "ORDER"})

    assert response.status_code == status_code
    assert response.json()["code"] == code
    assert fake_service.runs == []
