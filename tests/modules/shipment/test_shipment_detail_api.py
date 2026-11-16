import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security.cookies import ACCESS_TOKEN_COOKIE
from app.core.security.jwt import create_access_token
from app.main import app
from app.modules.shipment.application.exception.shipment_exceptions import (
    ShipmentNotFoundException,
)
from app.modules.shipment.application.shipment_detail_service import (
    ShipmentDetail,
    ShipmentDetailLine,
    ShipmentPlannedBasis,
    ShipmentScheduleObservation,
)
from app.modules.shipment.application.shipment_list_service import ShipmentListItem
from app.modules.shipment.dependencies import get_shipment_detail_service

"""
    선적 상세 API 라우터 시험 (DB 없음, 서비스 가짜)
      - 서비스·SQL 은 test_shipment_detail_sql.py 가 실제 DB 로 본다. 여기서는 역할·경로 파라미터·응답 모양·오류 코드.
"""

COMPANY_ID = uuid.uuid7()
SHIPMENT_ID = uuid.uuid7()
WAREHOUSE_ID = uuid.uuid7()
SKU_ID = uuid.uuid7()


def _url(shipment_id=SHIPMENT_ID) -> str:
    return f"/api/v1/shipments/{shipment_id}"


def _access_cookie(role_code: str, company_id: uuid.UUID | None = None) -> dict:
    token = create_access_token(
        user_id=uuid.uuid7(),
        role_code=role_code,
        company_id=company_id,
        config=get_settings().jwt_config,
    )
    return {ACCESS_TOKEN_COOKIE: token}


async def _get(url: str, cookies: dict | None = None):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", cookies=cookies
    ) as client:
        return await client.get(url)


@dataclass
class FakeSession:
    executed: list = field(default_factory=list)

    async def execute(self, statement, params=None):
        self.executed.append((str(statement), params))


def _detail() -> ShipmentDetail:
    return ShipmentDetail(
        item=ShipmentListItem(
            shipment_id=SHIPMENT_ID,
            shipment_code="OCN-20251229",
            transport_mode="OCEAN",
            route="KR → LOS ANGELES",
            warehouse_id=WAREHOUSE_ID,
            warehouse_code="WH-WEST",
            region_code="WEST",
            representative_sku_id=SKU_ID,
            representative_sku_code="SKU-001",
            representative_product_name="A Glow Hydra Toner Pad 60ea",
            sku_count=1,
            stage="RECEIVED",
            planned_kr_release_date=date(2026, 1, 10),
            planned_3pl_inbound_date=date(2026, 2, 11),
            expected_3pl_inbound_date=date(2026, 2, 13),
            actual_3pl_inbound_date=date(2026, 2, 13),
            is_delayed=True,
            delay_days=2,
        ),
        expected_port_arrival_date=date(2026, 2, 6),
        lines=[
            ShipmentDetailLine(
                line_no=1,
                sku_id=SKU_ID,
                sku_code="SKU-001",
                product_name="A Glow Hydra Toner Pad 60ea",
                shipped_quantity=1200,
            )
        ],
        planned_basis=ShipmentPlannedBasis(
            month="2026-01",
            us_port_name="LOS ANGELES, CA",
            total_leadtime_p50=Decimal("24.50"),
            total_leadtime_p90=Decimal("31.20"),
            confidence_leadtime="low",
        ),
        schedule_history=[
            ShipmentScheduleObservation(
                observed_at=datetime(2026, 1, 10, 0, 0, tzinfo=UTC),
                stage="PRE_DEPARTURE",
                expected_port_arrival_date=date(2026, 2, 4),
                expected_3pl_inbound_date=date(2026, 2, 11),
            ),
            ShipmentScheduleObservation(
                observed_at=datetime(2026, 2, 13, 0, 0, tzinfo=UTC),
                stage="RECEIVED",
                expected_port_arrival_date=None,
                expected_3pl_inbound_date=date(2026, 2, 13),
            ),
        ],
        data_basis_at=datetime(2026, 2, 13, 1, 0, tzinfo=UTC),
    )


class FakeShipmentDetailService:
    def __init__(self, raise_not_found: bool = False):
        self.calls: list = []
        self.raise_not_found = raise_not_found

    async def get(self, tenant, shipment_id):
        self.calls.append((tenant, shipment_id))
        if self.raise_not_found:
            raise ShipmentNotFoundException()
        return _detail()


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
    service = FakeShipmentDetailService()
    app.dependency_overrides[get_shipment_detail_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_shipment_detail_service, None)


async def test_쿠키가_없으면_401(fake_service):
    response = await _get(_url())

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHENTICATED"
    assert fake_service.calls == []


@pytest.mark.parametrize("role_code", ["OWNER", "ADMIN"])
async def test_고객사_범위가_없는_역할은_403(fake_service, role_code):
    response = await _get(_url(), cookies=_access_cookie(role_code))

    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
    assert fake_service.calls == []


async def test_선적_ID_가_UUID_가_아니면_422(fake_service):
    response = await _get(
        _url("not-a-uuid"), cookies=_access_cookie("CLIENT_MEMBER", COMPANY_ID)
    )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert fake_service.calls == []


async def test_없는_선적은_404_SHIPMENT_NOT_FOUND(fake_session):
    service = FakeShipmentDetailService(raise_not_found=True)
    app.dependency_overrides[get_shipment_detail_service] = lambda: service
    try:
        response = await _get(_url(), cookies=_access_cookie("CLIENT_LEAD", COMPANY_ID))
    finally:
        app.dependency_overrides.pop(get_shipment_detail_service, None)

    assert response.status_code == 404
    assert response.json()["code"] == "SHIPMENT_NOT_FOUND"


async def test_토큰의_고객사와_경로의_선적으로_서비스를_부르고_RLS_를_먼저_설정(
    fake_service, fake_session
):
    response = await _get(_url(), cookies=_access_cookie("CLIENT_MEMBER", COMPANY_ID))

    assert response.status_code == 200
    ((tenant, shipment_id),) = fake_service.calls
    assert tenant.company_id == COMPANY_ID
    assert shipment_id == SHIPMENT_ID
    assert any("set_config" in sql for sql, _ in fake_session.executed)


async def test_응답은_목록_필드_더하기_상세_필드이고_camelCase(fake_service):
    response = await _get(_url(), cookies=_access_cookie("CLIENT_MEMBER", COMPANY_ID))
    body = response.json()

    # 목록 행 필드 (명세 4.3 "목록 필드 +"), 3pl 표기는 명세 그대로
    for key in (
        "shipmentId",
        "shipmentCode",
        "transportMode",
        "route",
        "warehouse",
        "representativeSku",
        "skuCount",
        "stage",
        "plannedKrReleaseDate",
        "planned3plInboundDate",
        "expected3plInboundDate",
        "actual3plInboundDate",
        "isDelayed",
        "delayDays",
    ):
        assert key in body, key
    assert body["actual3plInboundDate"] == "2026-02-13"
    assert body["delayDays"] == 2 and body["isDelayed"] is True
    # 상세 필드
    assert body["expectedPortArrivalDate"] == "2026-02-06"
    assert body["lines"] == [
        {
            "lineNo": 1,
            "sku": {
                "skuId": str(SKU_ID),
                "skuCode": "SKU-001",
                "productName": "A Glow Hydra Toner Pad 60ea",
            },
            "shippedQuantity": 1200,
        }
    ]
    assert body["plannedBasis"] == {
        "month": "2026-01",
        "usPortName": "LOS ANGELES, CA",
        "totalLeadtimeP50": 24.5,
        "totalLeadtimeP90": 31.2,
        "confidenceLeadtime": "low",
    }
    assert [h["stage"] for h in body["scheduleHistory"]] == [
        "PRE_DEPARTURE",
        "RECEIVED",
    ]
    assert body["scheduleHistory"][1] == {
        "observedAt": "2026-02-13T00:00:00Z",
        "stage": "RECEIVED",
        "expectedPortArrivalDate": None,
        "expected3plInboundDate": "2026-02-13",
    }
    assert body["dataBasisAt"] == "2026-02-13T01:00:00Z"
