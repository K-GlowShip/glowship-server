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
from app.modules.inventory.application.inventory_list_service import (
    InventoryListItem,
    InventoryListResult,
)
from app.modules.inventory.dependencies import get_inventory_list_service

URL = "/api/v1/inventory"
COMPANY_ID = uuid.uuid7()
WAREHOUSE_ID = uuid.uuid7()
SKU_ID = uuid.uuid7()
RUN_ID = uuid.uuid7()


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


def _item(**overrides) -> InventoryListItem:
    values = {
        "inventory_date": date(2026, 6, 30),
        "warehouse_id": WAREHOUSE_ID,
        "warehouse_code": "WH-WEST",
        "region_code": "WEST",
        "sku_id": SKU_ID,
        "sku_code": "SKU-001",
        "product_name": "A Glow Hydra Toner Pad 60ea",
        "on_hand_quantity": 1240,
        "reserved_quantity": 310,
        "available_quantity": 930,
        "outbound_quantity": 85,
        "data_sync_run_id": RUN_ID,
    }
    values.update(overrides)
    return InventoryListItem(**values)


class FakeInventoryListService:
    """서비스는 단위 테스트에서 검증하므로, 라우터 테스트에서는 받은 인자만 기록하고 고정 결과를 돌려준다."""

    def __init__(self, inventory_date: date | None = date(2026, 6, 30)):
        self.calls: list = []
        self.inventory_date = inventory_date

    async def get_page(self, tenant, **kwargs):
        self.calls.append((tenant, kwargs))
        if self.inventory_date is None:
            return InventoryListResult(
                items=[], total_count=0, page=kwargs["page"], inventory_date=None
            )
        return InventoryListResult(
            items=[_item(), _item(sku_id=uuid.uuid7(), sku_code="SKU-002")],
            total_count=66,
            page=kwargs["page"],
            inventory_date=self.inventory_date,
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
    service = FakeInventoryListService()
    app.dependency_overrides[get_inventory_list_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_inventory_list_service, None)


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
        {"date": "2026-13-01"},
        {"date": "20260630"},
        {"date": "1782777600"},
        {"date": "0"},
        {"date": "2026-06-30T00:00:00Z"},
        {"warehouseId": "not-a-uuid"},
        {"skuId": "not-a-uuid"},
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
async def test_고객사_사용자는_200과_camelCase_목록_모양과_기준_일자를_받는다(
    fake_service, fake_session, role_code
):
    response = await _get(cookies=_access_cookie(role_code, company_id=COMPANY_ID))

    assert response.status_code == 200
    assert "set-cookie" not in response.headers

    body = response.json()
    assert list(body) == [
        "items",
        "page",
        "size",
        "totalCount",
        "totalPages",
        "inventoryDate",
    ]
    assert (body["page"], body["size"], body["totalCount"], body["totalPages"]) == (
        1,
        20,
        66,
        4,
    )
    assert body["inventoryDate"] == "2026-06-30"
    assert body["items"][0] == {
        "inventoryDate": "2026-06-30",
        "warehouse": {
            "warehouseId": str(WAREHOUSE_ID),
            "warehouseCode": "WH-WEST",
            "regionCode": "WEST",
        },
        "sku": {
            "skuId": str(SKU_ID),
            "skuCode": "SKU-001",
            "productName": "A Glow Hydra Toner Pad 60ea",
        },
        "onHandQuantity": 1240,
        "reservedQuantity": 310,
        "availableQuantity": 930,
        "outboundQuantity": 85,
        "dataSyncRunId": str(RUN_ID),
    }
    assert body["items"][1]["sku"]["skuCode"] == "SKU-002"


async def test_적재된_재고가_없으면_빈_목록과_null_기준_일자를_준다(fake_session):
    service = FakeInventoryListService(inventory_date=None)
    app.dependency_overrides[get_inventory_list_service] = lambda: service
    try:
        response = await _get(
            cookies=_access_cookie("CLIENT_MEMBER", company_id=COMPANY_ID)
        )
    finally:
        app.dependency_overrides.pop(get_inventory_list_service, None)

    body = response.json()
    assert response.status_code == 200
    assert body["items"] == [] and body["totalCount"] == 0
    assert body["totalPages"] == 0
    assert body["inventoryDate"] is None


async def test_기본값과_쿼리_파라미터를_서비스에_넘긴다(fake_service, fake_session):
    cookies = _access_cookie("CLIENT_MEMBER", company_id=COMPANY_ID)

    await _get(cookies=cookies)
    response = await _get(
        cookies=cookies,
        params={
            "date": "2025-01-01",
            "warehouseId": str(WAREHOUSE_ID),
            "skuId": str(SKU_ID),
            "q": "toner",
            "page": 2,
            "size": 50,
        },
    )

    [(_, default_kwargs), (tenant, kwargs)] = fake_service.calls
    assert isinstance(default_kwargs["page"], PageParams)
    assert (default_kwargs["page"].page, default_kwargs["page"].size) == (1, 20)
    assert {k: v for k, v in default_kwargs.items() if k != "page"} == {
        "inventory_date": None,
        "warehouse_id": None,
        "sku_id": None,
        "query": None,
    }

    assert tenant.company_id == COMPANY_ID
    assert {k: v for k, v in kwargs.items() if k != "page"} == {
        "inventory_date": date(2025, 1, 1),
        "warehouse_id": WAREHOUSE_ID,
        "sku_id": SKU_ID,
        "query": "toner",
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
