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
from app.modules.master_data.application.exception.sku_exceptions import (
    SkuNotFoundException,
)
from app.modules.master_data.application.sku_list_service import (
    SkuDetail,
    SkuListItem,
    SkuListResult,
    SkuValidationStatus,
)
from app.modules.master_data.dependencies import get_sku_list_service
from app.modules.master_data.infrastructure.repository.sku_repository import (
    AffectedDelayEventRow,
    ExternalCodeRow,
    SkuIssueRow,
)

URL = "/api/v1/skus"
COMPANY_ID = uuid.uuid7()
SKU_ID = uuid.uuid7()
ISSUE_ID = uuid.uuid7()
DELAY_EVENT_ID = uuid.uuid7()
SHIPMENT_ID = uuid.uuid7()
RUN_ID = uuid.uuid7()


def _access_cookie(role_code: str, company_id: uuid.UUID | None = None) -> dict:
    token = create_access_token(
        user_id=uuid.uuid7(),
        role_code=role_code,
        company_id=company_id,
        config=get_settings().jwt_config,
    )
    return {ACCESS_TOKEN_COOKIE: token}


async def _get(cookies: dict | None = None, params: dict | None = None, url: str = URL):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", cookies=cookies
    ) as client:
        return await client.get(url, params=params)


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
        "hs10": "3304995000",
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
        self.detail: SkuDetail | None = None

    async def get_detail(self, tenant, sku_id):
        self.calls.append((tenant, sku_id))
        if self.detail is None:
            raise SkuNotFoundException()
        return self.detail

    async def get_page(self, tenant, **kwargs):
        self.calls.append((tenant, kwargs))
        return SkuListResult(
            items=[
                _item(),
                _item(
                    sku_id=uuid.uuid7(),
                    sku_code="SKU-202",
                    external_codes=(
                        ExternalCodeRow("INVENTORY", "WMS-SKU-202", "MAPPED"),
                        ExternalCodeRow("SHIPMENT", None, "UNMAPPED"),
                    ),
                    validation_status=SkuValidationStatus.CODE_MISSING,
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
        "hs10": "3304995000",
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
    # 매핑이 없는 원천도 항목을 빼지 않고 externalCode = null 로 준다
    assert body["items"][1]["externalCodes"] == [
        {
            "sourceDomain": "INVENTORY",
            "externalCode": "WMS-SKU-202",
            "mappingStatus": "MAPPED",
        },
        {"sourceDomain": "SHIPMENT", "externalCode": None, "mappingStatus": "UNMAPPED"},
    ]
    assert body["items"][1]["validationStatus"] == "CODE_MISSING"


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


# --- 상품 상세 ------------------------------------------------------------------------------

DETAIL_URL = f"{URL}/{SKU_ID}"


async def test_상세는_쿠키가_없으면_401을_반환한다(fake_service, fake_session):
    response = await _get(url=DETAIL_URL)

    assert response.status_code == 401
    assert fake_service.calls == []


@pytest.mark.parametrize("role_code", ["OWNER", "ADMIN"])
async def test_상세는_고객사_범위가_없는_역할에_403을_반환한다(
    fake_service, fake_session, role_code
):
    response = await _get(cookies=_access_cookie(role_code), url=DETAIL_URL)

    assert response.status_code == 403
    assert fake_service.calls == []


async def test_상세의_skuId_가_UUID_가_아니면_422를_반환한다(
    fake_service, fake_session
):
    response = await _get(
        cookies=_access_cookie("CLIENT_MEMBER", company_id=COMPANY_ID),
        url=f"{URL}/not-a-uuid",
    )

    assert response.status_code == 422
    assert fake_service.calls == []


async def test_없는_상품은_404_SKU_NOT_FOUND_를_반환한다(fake_service, fake_session):
    response = await _get(
        cookies=_access_cookie("CLIENT_MEMBER", company_id=COMPANY_ID), url=DETAIL_URL
    )

    assert response.status_code == 404
    assert response.json()["code"] == "SKU_NOT_FOUND"


@pytest.mark.parametrize("role_code", ["CLIENT_MEMBER", "CLIENT_LEAD"])
async def test_상세는_목록_행에_이슈와_영향_지연을_더해_camelCase_로_준다(
    fake_service, fake_session, role_code
):
    fake_service.detail = SkuDetail(
        item=_item(),
        issues=[
            SkuIssueRow(
                data_validation_issue_id=ISSUE_ID,
                source_record_key="WMS-ATL01|SKU-201|2025-12-31",
                field_name=None,
                error_code="UNKNOWN_WAREHOUSE",
                error_message="등록되지 않은 창고 코드 WMS-ATL01",
                severity="ERROR",
                detected_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        ],
        affected_delay_events=[
            AffectedDelayEventRow(
                delay_event_id=DELAY_EVENT_ID,
                shipment_id=SHIPMENT_ID,
                shipment_code="GS-1024",
                analysis_run_id=RUN_ID,
                run_code="AN-260102-002",
                status="ON_HOLD",
                result_class=None,
                failure_code="REQUIRED_DATA_INVALID",
            )
        ],
    )

    response = await _get(
        cookies=_access_cookie(role_code, company_id=COMPANY_ID), url=DETAIL_URL
    )

    assert response.status_code == 200
    body = response.json()
    assert fake_service.calls[0][1] == SKU_ID
    list_item = await _list_item_body(fake_service, role_code)
    assert {k: body[k] for k in list_item} == list_item
    assert set(body) - set(list_item) == {"issues", "affectedDelayEvents"}
    assert body["issues"] == [
        {
            "dataValidationIssueId": str(ISSUE_ID),
            "sourceRecordKey": "WMS-ATL01|SKU-201|2025-12-31",
            "fieldName": None,
            "errorCode": "UNKNOWN_WAREHOUSE",
            "errorMessage": "등록되지 않은 창고 코드 WMS-ATL01",
            "severity": "ERROR",
            "detectedAt": "2026-01-01T00:00:00Z",
        }
    ]
    assert body["affectedDelayEvents"] == [
        {
            "delayEventId": str(DELAY_EVENT_ID),
            "shipment": {"shipmentId": str(SHIPMENT_ID), "shipmentCode": "GS-1024"},
            "currentRun": {
                "runId": str(RUN_ID),
                "runCode": "AN-260102-002",
                "status": "ON_HOLD",
                "resultClass": None,
                "failureCode": "REQUIRED_DATA_INVALID",
            },
        }
    ]


async def _list_item_body(fake_service, role_code) -> dict:
    response = await _get(cookies=_access_cookie(role_code, company_id=COMPANY_ID))
    return response.json()["items"][0]


async def test_상세는_이슈와_영향_지연이_없으면_빈_배열이다(fake_service, fake_session):
    fake_service.detail = SkuDetail(item=_item(), issues=[], affected_delay_events=[])

    response = await _get(
        cookies=_access_cookie("CLIENT_MEMBER", company_id=COMPANY_ID), url=DETAIL_URL
    )

    assert response.status_code == 200
    assert response.json()["issues"] == []
    assert response.json()["affectedDelayEvents"] == []


async def test_상세도_토큰의_고객사로_RLS_컨텍스트를_설정한다(
    fake_service, fake_session
):
    fake_service.detail = SkuDetail(item=_item(), issues=[], affected_delay_events=[])

    await _get(
        cookies=_access_cookie("CLIENT_MEMBER", company_id=COMPANY_ID), url=DETAIL_URL
    )

    assert str(COMPANY_ID) in str(fake_session.executed)
