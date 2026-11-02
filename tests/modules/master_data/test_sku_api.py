import uuid
from dataclasses import dataclass, field

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.core.database import get_db
from app.core.pagination import PageParams
from app.core.security.cookies import ACCESS_TOKEN_COOKIE
from app.core.security.jwt import create_access_token
from app.main import app
from app.modules.master_data.application.sku_list_service import (
    SkuListItem,
    SkuListResult,
    SkuValidationStatus,
)
from app.modules.master_data.dependencies import get_sku_list_service
from app.modules.master_data.infrastructure.repository.sku_repository import (
    ExternalCodeRow,
)

URL = "/api/v1/skus"
COMPANY_ID = uuid.uuid7()
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


def _item(**overrides) -> SkuListItem:
    values = {
        "sku_id": SKU_ID,
        "sku_code": "SKU-201",
        "product_name": "B Bloom Hydra Toner Pad 60ea",
        "brand_name": "B Bloom",
        "category": "SKINCARE",
        "subcategory": "Toner Pad",
        "popularity_tier": "HIT",
        "is_emergency_air_eligible": True,
        "unit_code": "EA",
        "units_per_case": 24,
        "status": "ACTIVE",
        "external_codes": (
            ExternalCodeRow("INVENTORY", "WMS-SKU-201", "MAPPED"),
            ExternalCodeRow("SHIPMENT", "TMS-SKU-201", "MAPPED"),
        ),
        "validation_status": SkuValidationStatus.ISSUE_OPEN,
    }
    values.update(overrides)
    return SkuListItem(**values)


class FakeSkuListService:
    """서비스는 단위 테스트에서 검증하므로, 라우터 테스트에서는 받은 인자만 기록하고 고정 결과를 돌려준다."""

    def __init__(self):
        self.calls: list = []

    async def get_page(self, tenant, **kwargs):
        self.calls.append((tenant, kwargs))
        return SkuListResult(
            items=[
                _item(),
                _item(
                    sku_id=uuid.uuid7(),
                    sku_code="SKU-202",
                    external_codes=(),
                    validation_status=SkuValidationStatus.OK,
                ),
            ],
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
    service = FakeSkuListService()
    app.dependency_overrides[get_sku_list_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_sku_list_service, None)


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
        {"onlyErrors": "maybe"},
    ],
)
async def test_쿼리_파라미터가_범위를_벗어나면_422를_반환한다(
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
    assert body["items"][0] == {
        "skuId": str(SKU_ID),
        "skuCode": "SKU-201",
        "productName": "B Bloom Hydra Toner Pad 60ea",
        "brandName": "B Bloom",
        "category": "SKINCARE",
        "subcategory": "Toner Pad",
        "popularityTier": "HIT",
        "isEmergencyAirEligible": True,
        "unitCode": "EA",
        "unitsPerCase": 24,
        "status": "ACTIVE",
        "externalCodes": [
            {
                "sourceDomain": "INVENTORY",
                "externalCode": "WMS-SKU-201",
                "mappingStatus": "MAPPED",
            },
            {
                "sourceDomain": "SHIPMENT",
                "externalCode": "TMS-SKU-201",
                "mappingStatus": "MAPPED",
            },
        ],
        "validationStatus": "ISSUE_OPEN",
    }
    assert body["items"][1]["externalCodes"] == []
    assert body["items"][1]["validationStatus"] == "OK"


async def test_기본값과_쿼리_파라미터를_서비스에_넘긴다(fake_service, fake_session):
    cookies = _access_cookie("CLIENT_MEMBER", company_id=COMPANY_ID)

    await _get(cookies=cookies)
    response = await _get(
        cookies=cookies,
        params={"q": "toner", "onlyErrors": "true", "page": 3, "size": 50},
    )

    [(_, default_kwargs), (tenant, kwargs)] = fake_service.calls
    assert default_kwargs["query"] is None
    assert default_kwargs["only_errors"] is False
    assert isinstance(default_kwargs["page"], PageParams)
    assert (default_kwargs["page"].page, default_kwargs["page"].size) == (1, 20)

    assert tenant.company_id == COMPANY_ID
    assert kwargs["query"] == "toner"
    assert kwargs["only_errors"] is True
    assert (kwargs["page"].page, kwargs["page"].size) == (3, 50)
    assert response.json()["page"] == 3
    assert response.json()["size"] == 50
    assert response.json()["totalPages"] == 1


async def test_토큰의_고객사로_RLS_컨텍스트를_설정한다(fake_service, fake_session):
    await _get(cookies=_access_cookie("CLIENT_MEMBER", company_id=COMPANY_ID))

    [(sql, params)] = fake_session.executed
    assert "set_config('app.company_id'" in sql
    assert params["company_id"] == str(COMPANY_ID)
