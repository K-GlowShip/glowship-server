import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.core.database import get_db
from app.core.pagination import PageParams
from app.core.security.cookies import ACCESS_TOKEN_COOKIE
from app.core.security.jwt import create_access_token
from app.main import app
from app.modules.order.application.order_list_service import (
    OrderLineItem,
    OrderListItem,
    OrderListResult,
)
from app.modules.order.dependencies import get_order_list_service

URL = "/api/v1/orders"
COMPANY_ID = uuid.uuid7()
ORDER_ID = uuid.uuid7()
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


def _line(**overrides) -> OrderLineItem:
    values = {
        "line_no": 1,
        "sku_id": SKU_ID,
        "sku_code": "SKU-001",
        "product_name": "A Glow Hydra Toner Pad 60ea",
        "ordered_quantity": 3,
        "shipped_quantity": 3,
        "line_status": "ACTIVE",
    }
    values.update(overrides)
    return OrderLineItem(**values)


def _item(**overrides) -> OrderListItem:
    values = {
        "order_id": ORDER_ID,
        "order_code": "A-ORD-20260630-0177",
        "sales_channel": "AMAZON",
        "customer_region": "EAST",
        "warehouse_id": WAREHOUSE_ID,
        "warehouse_code": "WH-EAST",
        "region_code": "EAST",
        "ordered_at": datetime(2026, 6, 30, 23, 31, 48, tzinfo=UTC),
        "promised_delivery_at": datetime(2026, 7, 3, 23, 59, 59, tzinfo=UTC),
        "status": "OPEN",
        "lines": (_line(), _line(line_no=2, shipped_quantity=0)),
    }
    values.update(overrides)
    return OrderListItem(**values)


class FakeOrderListService:
    """서비스는 단위 테스트에서 검증하므로, 라우터 테스트에서는 받은 인자만 기록하고 고정 결과를 돌려준다."""

    def __init__(self):
        self.calls: list = []

    async def get_page(self, tenant, **kwargs):
        self.calls.append((tenant, kwargs))
        return OrderListResult(
            items=[_item(), _item(order_id=uuid.uuid7(), lines=())],
            total_count=20,
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
    service = FakeOrderListService()
    app.dependency_overrides[get_order_list_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_order_list_service, None)


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
        {"status": "PENDING"},
        {"salesChannel": "COUPANG"},
        {"warehouseId": "not-a-uuid"},
        {"from": "2026-13-01T00:00:00Z"},
        {"to": "not-a-datetime"},
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
async def test_고객사_사용자는_200과_camelCase_공통_목록_모양을_받는다(
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
        20,
        1,
    )
    sku = {
        "skuId": str(SKU_ID),
        "skuCode": "SKU-001",
        "productName": "A Glow Hydra Toner Pad 60ea",
    }
    assert body["items"][0] == {
        "orderId": str(ORDER_ID),
        "orderCode": "A-ORD-20260630-0177",
        "salesChannel": "AMAZON",
        "customerRegion": "EAST",
        "warehouse": {
            "warehouseId": str(WAREHOUSE_ID),
            "warehouseCode": "WH-EAST",
            "regionCode": "EAST",
        },
        "orderedAt": "2026-06-30T23:31:48Z",
        "promisedDeliveryAt": "2026-07-03T23:59:59Z",
        "status": "OPEN",
        "lines": [
            {
                "lineNo": 1,
                "sku": sku,
                "orderedQuantity": 3,
                "shippedQuantity": 3,
                "lineStatus": "ACTIVE",
            },
            {
                "lineNo": 2,
                "sku": sku,
                "orderedQuantity": 3,
                "shippedQuantity": 0,
                "lineStatus": "ACTIVE",
            },
        ],
    }
    # 상세가 없는 주문도 lines 를 빼지 않고 빈 배열로 준다 (응답 규칙 4절)
    assert body["items"][1]["lines"] == []


async def test_기본값과_쿼리_파라미터를_서비스에_넘긴다(fake_service, fake_session):
    cookies = _access_cookie("CLIENT_MEMBER", company_id=COMPANY_ID)

    await _get(cookies=cookies)
    response = await _get(
        cookies=cookies,
        params={
            "status": "OPEN",
            "warehouseId": str(WAREHOUSE_ID),
            "salesChannel": "SEPHORA",
            "from": "2026-05-31T15:00:00Z",
            "to": "2026-06-30T15:00:00+00:00",
            "q": "ORD-2026",
            "page": 3,
            "size": 50,
        },
    )

    [(_, default_kwargs), (tenant, kwargs)] = fake_service.calls
    assert isinstance(default_kwargs["page"], PageParams)
    assert (default_kwargs["page"].page, default_kwargs["page"].size) == (1, 20)
    assert {k: v for k, v in default_kwargs.items() if k != "page"} == {
        "status": None,
        "warehouse_id": None,
        "sales_channel": None,
        "ordered_from": None,
        "ordered_to": None,
        "query": None,
    }

    assert tenant.company_id == COMPANY_ID
    assert {k: v for k, v in kwargs.items() if k != "page"} == {
        "status": "OPEN",
        "warehouse_id": WAREHOUSE_ID,
        "sales_channel": "SEPHORA",
        "ordered_from": datetime(2026, 5, 31, 15, 0, tzinfo=UTC),
        "ordered_to": datetime(2026, 6, 30, 15, 0, tzinfo=UTC),
        "query": "ORD-2026",
    }
    assert (kwargs["page"].page, kwargs["page"].size) == (3, 50)
    assert response.json()["page"] == 3
    assert response.json()["size"] == 50
    assert response.json()["totalPages"] == 1


async def test_토큰의_고객사로_RLS_컨텍스트를_설정한다(fake_service, fake_session):
    await _get(cookies=_access_cookie("CLIENT_MEMBER", company_id=COMPANY_ID))

    [(sql, params)] = fake_session.executed
    assert "set_config('app.company_id'" in sql
    assert params["company_id"] == str(COMPANY_ID)
