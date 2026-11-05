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
from app.modules.delay_analysis.infrastructure.analysis_enums import SalesBasisType
from app.modules.inventory.application.inventory_status_rules import InventoryStatus
from app.modules.inventory.application.inventory_status_service import (
    InventoryStatusItem,
    InventoryStatusResult,
)
from app.modules.inventory.dependencies import get_inventory_status_service

URL = "/api/v1/inventory-status"
COMPANY_ID = uuid.uuid7()
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
    executed: list = field(default_factory=list)

    async def execute(self, statement, params=None):
        self.executed.append((str(statement), params))


class FakeInventoryStatusService:
    def __init__(self):
        self.calls: list = []

    async def get_page(self, tenant, **kwargs):
        self.calls.append((tenant, kwargs))
        return InventoryStatusResult(
            items=[
                InventoryStatusItem(
                    warehouse_id=WAREHOUSE_ID,
                    warehouse_code="WH-WEST",
                    region_code="WEST",
                    sku_id=SKU_ID,
                    sku_code="SKU-001",
                    product_name="A Glow Hydra Toner Pad 60ea",
                    available_quantity=930,
                    average_daily_sales=18.535714,
                    safety_stock_breach_date=date(2026, 7, 4),
                    stockout_date=None,
                    status=InventoryStatus.CAUTION,
                )
            ],
            total_count=66,
            page=kwargs["page"],
            inventory_date=date(2026, 6, 30),
            operating_rule_version_no=1,
            sales_basis_days=28,
            safety_stock_days=14,
            sales_basis_type=SalesBasisType.ORDER_DEMAND,
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
    service = FakeInventoryStatusService()
    app.dependency_overrides[get_inventory_status_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_inventory_status_service, None)


async def test_쿠키가_없으면_401(fake_service):
    response = await _get()

    assert response.status_code == 401
    assert fake_service.calls == []


@pytest.mark.parametrize("role_code", ["OWNER", "ADMIN"])
async def test_고객사_범위가_없는_역할은_403(fake_service, role_code):
    response = await _get(cookies=_access_cookie(role_code))

    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
    assert fake_service.calls == []


@pytest.mark.parametrize(
    "params",
    [{"warehouseId": "not-a-uuid"}, {"page": 0}, {"size": 0}, {"size": 101}],
)
async def test_쿼리_파라미터가_형식에_어긋나면_422(fake_service, params):
    response = await _get(
        cookies=_access_cookie("CLIENT_MEMBER", company_id=COMPANY_ID), params=params
    )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert fake_service.calls == []


@pytest.mark.parametrize("role_code", ["CLIENT_MEMBER", "CLIENT_LEAD"])
async def test_목록_모양과_계산_기준을_camelCase_로_돌려준다(
    fake_service, fake_session, role_code
):
    response = await _get(cookies=_access_cookie(role_code, company_id=COMPANY_ID))

    assert response.status_code == 200
    body = response.json()
    assert list(body) == [
        "items",
        "page",
        "size",
        "totalCount",
        "totalPages",
        "inventoryDate",
        "operatingRuleVersionNo",
        "salesBasisDays",
        "safetyStockDays",
        "salesBasisType",
    ]
    assert (body["totalCount"], body["totalPages"]) == (66, 4)
    assert (
        body["inventoryDate"],
        body["operatingRuleVersionNo"],
        body["salesBasisDays"],
        body["safetyStockDays"],
        body["salesBasisType"],
    ) == ("2026-06-30", 1, 28, 14, "ORDER_DEMAND")
    assert body["items"][0] == {
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
        "availableQuantity": 930,
        "averageDailySales": 18.535714,
        "safetyStockBreachDate": "2026-07-04",
        "stockoutDate": None,
        "status": "CAUTION",
    }
    assert fake_session.executed, "RLS 컨텍스트 SQL 이 실행되어야 한다"


async def test_창고와_페이지를_서비스에_넘긴다(fake_service):
    await _get(
        cookies=_access_cookie("CLIENT_LEAD", company_id=COMPANY_ID),
        params={"warehouseId": str(WAREHOUSE_ID), "page": 2, "size": 10},
    )

    tenant, kwargs = fake_service.calls[0]
    assert tenant.company_id == COMPANY_ID
    assert kwargs["warehouse_id"] == WAREHOUSE_ID
    assert (kwargs["page"].page, kwargs["page"].size) == (2, 10)
    assert isinstance(kwargs["page"], PageParams)
