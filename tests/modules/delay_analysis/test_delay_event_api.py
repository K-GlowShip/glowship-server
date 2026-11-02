import uuid
from datetime import UTC, date, datetime

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.core.security.cookies import ACCESS_TOKEN_COOKIE
from app.core.security.jwt import create_access_token
from app.main import app
from app.modules.delay_analysis.application.delay_event_query_service import (
    DelayEventDetail,
    DelayEventList,
    DelayEventListItem,
    DelayEventTab,
    ShipmentSummary,
)
from app.modules.delay_analysis.application.exception.delay_event_exceptions import (
    DelayEventNotFoundException,
)
from app.modules.delay_analysis.dependencies import get_delay_event_query_service
from app.modules.delay_analysis.infrastructure.repository.delay_event_read_repository import (
    DelayEventCounts,
    DelayEventRow,
    RunHistoryRow,
)

COMPANY_ID = uuid.uuid7()
EVENT_ID = uuid.uuid7()
RUN_ID = uuid.uuid7()
ROW = DelayEventRow(
    delay_event_id=EVENT_ID,
    shipment_id=uuid.uuid7(),
    stage="AT_SEA",
    planned_3pl_inbound_date=date(2026, 1, 27),
    expected_3pl_inbound_date=date(2026, 1, 30),
    detected_at=datetime(2026, 1, 20, tzinfo=UTC),
    closed_at=None,
    previous_shipment_schedule_snapshot_id=None,
    shipment_code="OCN-20251215",
    transport_mode="OCEAN",
    warehouse_id=uuid.uuid7(),
    us_port_name="LOS ANGELES, CA",
    origin_region_code="KR",
    run_id=RUN_ID,
    run_status="READY_FOR_REVIEW",
    has_final_decision=False,
    at_risk_order_count=4,
)
SHIPMENT = ShipmentSummary(
    route="KR → LOS ANGELES",
    representative_sku="세럼",
    representative_sku_code="SKU-002",
    sku_count=3,
)


def _cookie(role_code: str) -> dict:
    token = create_access_token(
        user_id=uuid.uuid7(),
        role_code=role_code,
        company_id=None if role_code in ("ADMIN", "OWNER") else COMPANY_ID,
        config=get_settings().jwt_config,
    )
    return {ACCESS_TOKEN_COOKIE: token}


async def _get(path: str, params: dict | None = None, role: str | None = "CLIENT_LEAD"):
    cookies = _cookie(role) if role else None
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", cookies=cookies
    ) as client:
        return await client.get(path, params=params)


class FakeQueryService:
    """서비스는 단위 테스트에서 검증하므로, 라우터 테스트에서는 호출 인자만 기록하고 고정 결과를 돌려준다."""

    def __init__(self):
        self.calls: list[dict] = []

    async def list_delay_events(self, **kwargs):
        self.calls.append(kwargs)
        return DelayEventList(
            items=[DelayEventListItem(row=ROW, shipment=SHIPMENT, delay_days=3)],
            counts=DelayEventCounts(
                all_count=1,
                pending_decision=1,
                analyzing=0,
                on_hold=0,
                needs_reanalysis=0,
            ),
        )

    async def get_delay_event(self, *, actor, delay_event_id):
        if delay_event_id != EVENT_ID:
            raise DelayEventNotFoundException()
        run = RunHistoryRow(
            analysis_run_id=RUN_ID,
            version_no=1,
            status="READY_FOR_REVIEW",
            is_latest=True,
            trigger_type="AUTO",
            started_at=datetime(2026, 1, 20, tzinfo=UTC),
            finished_at=None,
        )
        return DelayEventDetail(
            row=ROW,
            shipment=SHIPMENT,
            delay_days=3,
            previous_expected_3pl_inbound_date=date(2026, 1, 28),
            runs=[run],
        )


@pytest.fixture
def fake_service():
    service = FakeQueryService()
    app.dependency_overrides[get_delay_event_query_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_delay_event_query_service, None)


# --- 인증·권한 -------------------------------------------------------------------


async def test_list_without_cookie_returns_401(fake_service):
    response = await _get("/api/v1/delay-events", role=None)

    assert response.status_code == 401


@pytest.mark.parametrize("role", ["ADMIN", "OWNER"])
async def test_list_for_non_client_role_returns_403(fake_service, role):
    response = await _get("/api/v1/delay-events", role=role)

    assert response.status_code == 403


# --- API 15 목록 -----------------------------------------------------------------


async def test_list_returns_items_in_spec_field_names(fake_service):
    response = await _get("/api/v1/delay-events")

    assert response.status_code == 200
    body = response.json()
    [item] = body["items"]
    assert item["planned3plInboundDate"] == "2026-01-27"
    assert item["expected3plInboundDate"] == "2026-01-30"
    assert item["route"] == "KR → LOS ANGELES"
    assert item["representativeSku"] == "세럼"
    assert item["delayDays"] == 3
    assert item["runId"] == str(RUN_ID)
    assert item["runCode"] is None
    assert item["atRiskOrderCount"] == 4
    assert body["counts"] == {
        "all": 1,
        "pendingDecision": 1,
        "analyzing": 0,
        "onHold": 0,
        "needsReanalysis": 0,
    }


async def test_list_passes_query_params_to_service(fake_service):
    warehouse_id = uuid.uuid7()

    await _get(
        "/api/v1/delay-events",
        params={
            "status": "NEEDS_REANALYSIS",
            "warehouseId": str(warehouse_id),
            "from": "2026-01-01",
            "to": "2026-01-31",
            "q": "OCN",
            "hasFinalDecision": "false",
        },
    )

    [call] = fake_service.calls
    assert call["tab"] == DelayEventTab.NEEDS_REANALYSIS
    assert call["warehouse_id"] == warehouse_id
    assert (call["detected_from"], call["detected_to"]) == (
        date(2026, 1, 1),
        date(2026, 1, 31),
    )
    assert call["q"] == "OCN"
    assert call["has_final_decision"] is False
    assert call["actor"].company_id == COMPANY_ID


async def test_list_rejects_unknown_tab(fake_service):
    response = await _get("/api/v1/delay-events", params={"status": "DONE"})

    assert response.status_code == 422


# --- API 16 상세 -----------------------------------------------------------------


async def test_detail_returns_schedule_and_run_history(fake_service):
    response = await _get(f"/api/v1/delay-events/{EVENT_ID}")

    assert response.status_code == 200
    body = response.json()
    assert body["shipment"]["shipmentCode"] == "OCN-20251215"
    assert body["previousExpected3plInboundDate"] == "2026-01-28"
    assert body["runs"][0]["versionNo"] == 1
    assert body["runs"][0]["runCode"] is None


async def test_detail_of_unknown_event_returns_404(fake_service):
    response = await _get(f"/api/v1/delay-events/{uuid.uuid7()}")

    assert response.status_code == 404
    assert response.json()["code"] == "DELAY_EVENT_NOT_FOUND"
