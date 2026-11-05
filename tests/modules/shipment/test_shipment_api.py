import uuid
from dataclasses import dataclass, field
from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.core.database import get_db
from app.core.pagination import PageParams
from app.core.security.cookies import ACCESS_TOKEN_COOKIE
from app.core.security.jwt import create_access_token
from app.main import app
from app.modules.shipment.application.shipment_list_service import (
    ShipmentListItem,
    ShipmentListResult,
)
from app.modules.shipment.dependencies import get_shipment_list_service

URL = "/api/v1/shipments"
COMPANY_ID = uuid.uuid7()
SHIPMENT_ID = uuid.uuid7()
WAREHOUSE_ID = uuid.uuid7()
SKU_ID = uuid.uuid7()


def _access_cookie(role_code: str, company_id: uuid.UUID | None = None) -> dict:
    token = create_access_token(
        user_id=uuid.uuid7(),
        role_code=role_code,
        company_id=company_id,
        config=get_settings().jwt_config,
    )
    return {ACCESS_TOKEN_COOKIE: token}


async def _get(cookies: dict | None = None, params: dict | None = None):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", cookies=cookies
    ) as client:
        return await client.get(URL, params=params)


@dataclass
class FakeSession:
    """DB 없이 실행한다. get_tenant_context 가 RLS 컨텍스트 SQL 을 실제로 실행하는지 기록한다."""

    executed: list = field(default_factory=list)

    async def execute(self, statement, params=None):
        self.executed.append((str(statement), params))


def _item(**overrides) -> ShipmentListItem:
    values = {
        "shipment_id": SHIPMENT_ID,
        "shipment_code": "OCN-20260601",
        "transport_mode": "OCEAN",
        "route": "KR → LOS ANGELES",
        "warehouse_id": WAREHOUSE_ID,
        "warehouse_code": "WH-WEST",
        "region_code": "WEST",
        "representative_sku_id": SKU_ID,
        "representative_sku_code": "SKU-001",
        "representative_product_name": "A Glow Hydra Toner Pad 60ea",
        "sku_count": 20,
        "stage": "AT_SEA",
        "planned_kr_release_date": date(2026, 6, 17),
        "planned_3pl_inbound_date": date(2026, 7, 12),
        "expected_3pl_inbound_date": date(2026, 7, 6),
        "actual_3pl_inbound_date": None,
        "is_delayed": False,
        "delay_days": 0,
    }
    values.update(overrides)
    return ShipmentListItem(**values)


class FakeShipmentListService:
    """서비스는 단위 테스트에서 검증하므로, 라우터 테스트에서는 받은 인자만 기록하고 고정 결과를 돌려준다."""

    def __init__(self):
        self.calls: list = []

    async def get_page(self, tenant, **kwargs):
        self.calls.append((tenant, kwargs))
        return ShipmentListResult(
            items=[
                _item(),
                _item(
                    shipment_id=uuid.uuid7(),
                    shipment_code="OCN-20251229",
                    stage="RECEIVED",
                    planned_3pl_inbound_date=date(2026, 2, 11),
                    expected_3pl_inbound_date=date(2026, 2, 13),
                    actual_3pl_inbound_date=date(2026, 2, 13),
                    is_delayed=True,
                    delay_days=2,
                    representative_sku_id=None,
                    representative_sku_code=None,
                    representative_product_name=None,
                    sku_count=0,
                ),
            ],
            total_count=83,
            page=kwargs["page"],
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
    service = FakeShipmentListService()
    app.dependency_overrides[get_shipment_list_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_shipment_list_service, None)


async def test_쿠키가_없으면_401을_반환한다(fake_service, fake_session):
    response = await _get()

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHENTICATED"
    assert fake_service.calls == []
    assert fake_session.executed == []


@pytest.mark.parametrize("role_code", ["OWNER", "ADMIN"])
async def test_고객사_범위가_없는_역할은_403을_반환한다(
    fake_service, fake_session, role_code
):
    response = await _get(cookies=_access_cookie(role_code))

    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
    assert fake_service.calls == []


@pytest.mark.parametrize(
    "params",
    [
        {"page": 0},
        {"size": 0},
        {"size": 101},
        {"q": "x" * 101},
        {"stage": "DELIVERED"},
        {"stage": "at_sea"},
        {"transportMode": "TRUCK"},
        {"warehouseId": "not-a-uuid"},
    ],
)
async def test_쿼리_파라미터가_형식에_어긋나면_422를_반환한다(
    fake_service, fake_session, params
):
    response = await _get(
        cookies=_access_cookie("CLIENT_MEMBER", company_id=COMPANY_ID), params=params
    )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert fake_service.calls == []


@pytest.mark.parametrize("role_code", ["CLIENT_MEMBER", "CLIENT_LEAD"])
async def test_고객사_사용자는_200과_camelCase_목록_모양을_받는다(
    fake_service, fake_session, role_code
):
    response = await _get(cookies=_access_cookie(role_code, company_id=COMPANY_ID))

    assert response.status_code == 200
    assert "set-cookie" not in response.headers

    body = response.json()
    assert list(body) == ["items", "page", "size", "totalCount", "totalPages"]
    assert (body["page"], body["size"], body["totalCount"], body["totalPages"]) == (
        1,
        20,
        83,
        5,
    )
    assert body["items"][0] == {
        "shipmentId": str(SHIPMENT_ID),
        "shipmentCode": "OCN-20260601",
        "transportMode": "OCEAN",
        "route": "KR → LOS ANGELES",
        "warehouse": {
            "warehouseId": str(WAREHOUSE_ID),
            "warehouseCode": "WH-WEST",
            "regionCode": "WEST",
        },
        "representativeSku": {
            "skuId": str(SKU_ID),
            "skuCode": "SKU-001",
            "productName": "A Glow Hydra Toner Pad 60ea",
        },
        "skuCount": 20,
        "stage": "AT_SEA",
        "plannedKrReleaseDate": "2026-06-17",
        "planned3plInboundDate": "2026-07-12",
        "expected3plInboundDate": "2026-07-06",
        "actual3plInboundDate": None,
        "isDelayed": False,
        "delayDays": 0,
    }


async def test_상세가_없는_선적은_대표_상품_null_과_품목_수_0_이고_지연_값이_그대로_나간다(
    fake_service, fake_session
):
    response = await _get(
        cookies=_access_cookie("CLIENT_MEMBER", company_id=COMPANY_ID)
    )

    received = response.json()["items"][1]
    assert received["representativeSku"] is None
    assert received["skuCount"] == 0
    assert received["stage"] == "RECEIVED"
    assert received["actual3plInboundDate"] == "2026-02-13"
    assert (received["isDelayed"], received["delayDays"]) == (True, 2)


async def test_기본값과_쿼리_파라미터를_서비스에_넘긴다(fake_service, fake_session):
    cookies = _access_cookie("CLIENT_MEMBER", company_id=COMPANY_ID)

    await _get(cookies=cookies)
    response = await _get(
        cookies=cookies,
        params={
            "stage": "PRE_DEPARTURE",
            "transportMode": "AIR",
            "warehouseId": str(WAREHOUSE_ID),
            "q": "air-reg",
            "page": 2,
            "size": 50,
        },
    )

    [(_, default_kwargs), (tenant, kwargs)] = fake_service.calls
    assert isinstance(default_kwargs["page"], PageParams)
    assert (default_kwargs["page"].page, default_kwargs["page"].size) == (1, 20)
    assert {k: v for k, v in default_kwargs.items() if k != "page"} == {
        "stage": None,
        "transport_mode": None,
        "warehouse_id": None,
        "query": None,
    }

    assert tenant.company_id == COMPANY_ID
    assert {k: v for k, v in kwargs.items() if k != "page"} == {
        "stage": "PRE_DEPARTURE",
        "transport_mode": "AIR",
        "warehouse_id": WAREHOUSE_ID,
        "query": "air-reg",
    }
    assert (kwargs["page"].page, kwargs["page"].size) == (2, 50)
    assert response.json()["page"] == 2
    assert response.json()["size"] == 50
    assert response.json()["totalPages"] == 2


async def test_토큰의_고객사로_RLS_컨텍스트를_설정한다(fake_service, fake_session):
    await _get(cookies=_access_cookie("CLIENT_MEMBER", company_id=COMPANY_ID))

    [(sql, params)] = fake_session.executed
    assert "set_config('app.company_id'" in sql
    assert params["company_id"] == str(COMPANY_ID)
