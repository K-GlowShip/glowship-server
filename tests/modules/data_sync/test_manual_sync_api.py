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
    SyncInProgressException,
)
from app.modules.data_sync.application.ingest.ingest_service import StartedRun
from app.modules.data_sync.application.manual_sync_service import TickTicket
from app.modules.data_sync.application.virtual_clock import TICK_ORDER, TickPlan
from app.modules.data_sync.dependencies import get_manual_sync_service

"""
    수동 동기화 API 25 라우터 테스트 (POST /data-sync-runs, 2026-11-09 결정). 서비스는 SQL 시험이 검증하므로
    여기서는 역할, 요청 형식(sourceSystem·sourceDomain 하나만, advance), 202 응답 배열, 백그라운드 실행 등록, 오류 코드 전달을 본다.
"""

COMPANY_ID = uuid.uuid7()
RUN_IDS = {domain: uuid.uuid7() for domain in TICK_ORDER}
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
    def __init__(self, virtual_start=AT):
        self.virtual_start = virtual_start
        self.requests: list = []
        self.ticks: list = []
        self.days: list = []
        self.error: Exception | None = None

    async def request_tick(self, tenant):
        self.requests.append(tenant)
        if self.error is not None:
            raise self.error
        return TickTicket(
            plan=TickPlan(at=AT, windows={}),
            started=tuple(
                StartedRun(tenant.company_id, RUN_IDS[d], d, AT) for d in TICK_ORDER
            ),
            company_code="A_BEAUTY",
        )

    async def run_tick(self, ticket):
        self.ticks.append(ticket)

    async def run_until_midnight(self, tenant, ticket):
        self.days.append((tenant, ticket))


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


EXPECTED = [
    {"sourceDomain": d.value, "dataSyncRunId": str(RUN_IDS[d]), "status": "RUNNING"}
    for d in TICK_ORDER
]


@pytest.mark.parametrize("role", ["CLIENT_MEMBER", "CLIENT_LEAD"])
@pytest.mark.parametrize(
    "body",
    [
        {"sourceSystem": "ERP"},
        {"sourceSystem": "WMS", "advance": "TICK"},
        {"sourceSystem": "TMS"},
        {"sourceDomain": "SHIPMENT"},  # 이전 요청 모양(하위 호환)
    ],
)
async def test_any_card_syncs_all_five_sources_and_gets_202_array(
    fake_service, role, body
):
    response = await _post(body, role=role)

    assert response.status_code == 202
    assert response.json() == EXPECTED  # 상품 → 창고 → 주문 → 선적 → 재고
    [tenant] = fake_service.requests
    assert tenant.company_id == COMPANY_ID
    # 응답 뒤 백그라운드로 tick 한 번을 적용한다
    [ticket] = fake_service.ticks
    assert fake_service.days == []
    assert len(ticket.started) == 5


async def test_next_day_runs_until_midnight_in_background(fake_service):
    response = await _post({"sourceSystem": "ERP", "advance": "NEXT_DAY"})

    assert response.status_code == 202
    assert response.json() == EXPECTED  # 첫 tick 의 다섯 수집
    assert fake_service.ticks == []
    [(tenant, ticket)] = fake_service.days
    assert tenant.company_id == COMPANY_ID and len(ticket.started) == 5


async def test_next_day_without_virtual_clock_returns_422(fake_service):
    fake_service.virtual_start = None

    response = await _post({"sourceSystem": "ERP", "advance": "NEXT_DAY"})

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert fake_service.requests == []


async def test_without_cookie_returns_401(fake_service):
    response = await _post({"sourceSystem": "TMS"}, role=None)

    assert response.status_code == 401
    assert fake_service.requests == []


@pytest.mark.parametrize("role", ["ADMIN", "OWNER"])
async def test_non_client_returns_403(fake_service, role):
    response = await _post({"sourceSystem": "TMS"}, role=role)

    assert response.status_code == 403
    assert fake_service.requests == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"sourceDomain": "RAIL"},
        {"sourceDomain": None},
        {"sourceSystem": "SAP"},
        {"sourceSystem": "ERP", "sourceDomain": "ORDER"},  # 둘 다
        {"advance": "TICK"},  # 둘 다 없음
        {"sourceSystem": "ERP", "advance": "NEXT_WEEK"},
    ],
)
async def test_invalid_body_returns_422(fake_service, body):
    response = await _post(body)

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert fake_service.requests == []


async def test_sync_in_progress_is_409_without_background_run(fake_service):
    fake_service.error = SyncInProgressException()

    response = await _post({"sourceSystem": "WMS"})

    assert response.status_code == 409
    assert response.json()["code"] == "SYNC_IN_PROGRESS"
    assert fake_service.ticks == [] and fake_service.days == []
