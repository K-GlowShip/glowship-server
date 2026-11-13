import uuid
from datetime import UTC, date, datetime

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.core.exceptions import ForbiddenException
from app.core.security.cookies import ACCESS_TOKEN_COOKIE
from app.core.security.jwt import create_access_token
from app.main import app
from app.modules.company.application.exception.company_exceptions import (
    CompanyInactiveException,
    CompanyNotFoundException,
)
from app.modules.operating_rule.application.exception.operating_rule_exceptions import (
    InvalidLeadTimeException,
)
from app.modules.operating_rule.application.operating_rule_service import (
    CreatedRuleSet,
    RuleSetDetail,
)
from app.modules.operating_rule.dependencies import get_operating_rule_service
from app.modules.operating_rule.infrastructure.repository.operating_rule_repository import (
    LeadTimeRow,
    NewLeadTime,
    RuleSetRow,
)

"""
    운영 기준 API 라우터 테스트 (API 4·5, GLOW-153). DB 없이 실행
      - 서비스는 dependency_overrides 로 대체하고, 역할 제한·형식 검증·응답 모양·서비스 예외의 상태 코드 변환을 확인한다.
"""

URL = "/api/v1/admin/operating-rule-sets"
COMPANY_ID = uuid.uuid7()
WAREHOUSE_ID = uuid.uuid7()
RULE_SET_ID = uuid.uuid7()
AUTHOR_ID = uuid.uuid7()


def _body(**overrides) -> dict:
    body = {
        "companyId": str(COMPANY_ID),
        "effectiveDate": "2026-07-03",
        "safetyStockDays": 14,
        "salesBasisDays": 28,
        "isPartialFulfillmentAllowed": False,
        "leadTimes": [
            {
                "leadTimeType": "AIR_TRANSIT",
                "warehouseId": str(WAREHOUSE_ID),
                "toRegionCode": "WEST",
                "days": 5,
                "dayBasis": "CALENDAR_DAY",
            },
            {
                "leadTimeType": "OUTBOUND_HANDLING",
                "warehouseId": str(WAREHOUSE_ID),
                "days": 1,
                "dayBasis": "CALENDAR_DAY",
            },
        ],
    }
    return {**body, **overrides}


def _lead_time(**overrides) -> dict:
    return {**_body()["leadTimes"][0], **overrides}


def _cookie(role_code: str | None) -> dict | None:
    if role_code is None:
        return None
    token = create_access_token(
        user_id=uuid.uuid7(),
        role_code=role_code,
        company_id=COMPANY_ID if role_code.startswith("CLIENT") else None,
        config=get_settings().jwt_config,
    )
    return {ACCESS_TOKEN_COOKIE: token}


async def _request(method: str, role: str | None = "ADMIN", **kwargs):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", cookies=_cookie(role)
    ) as client:
        return await client.request(method, URL, **kwargs)


async def _get(role: str | None = "ADMIN", params: dict | None = None):
    if params is None:
        params = {"companyId": str(COMPANY_ID)}
    return await _request("GET", role, params=params)


async def _post(body: dict | None = None, role: str | None = "ADMIN"):
    return await _request("POST", role, json=_body() if body is None else body)


def _detail() -> RuleSetDetail:
    return RuleSetDetail(
        row=RuleSetRow(
            operating_rule_set_id=RULE_SET_ID,
            version_no=2,
            effective_from=date(2026, 7, 3),
            safety_stock_days=14,
            sales_basis_days=28,
            allow_partial_fulfillment=None,
            created_at=datetime(2026, 7, 2, 0, 3, tzinfo=UTC),
            author_user_id=AUTHOR_ID,
            author_display_name=None,
            author_role_code=None,
        ),
        is_current=True,
        lead_times=[
            LeadTimeRow(
                operating_rule_set_id=RULE_SET_ID,
                lead_time_type="AIR_TRANSIT",
                warehouse_id=WAREHOUSE_ID,
                warehouse_code="WH-KR-01",
                warehouse_region_code="KR",
                to_region_code="WEST",
                days=5,
                day_basis="CALENDAR_DAY",
            )
        ],
    )


class FakeOperatingRuleService:
    """서비스는 단위·SQL 테스트에서 검증하므로, 라우터 테스트에서는 호출 인자만 기록한다."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.error: Exception | None = None

    async def list_rule_sets(self, **kwargs):
        self.calls.append(("list", kwargs))
        if self.error is not None:
            raise self.error
        return [_detail()]

    async def create_rule_set(self, **kwargs):
        self.calls.append(("create", kwargs))
        if self.error is not None:
            raise self.error
        return CreatedRuleSet(operating_rule_set_id=RULE_SET_ID, version_no=2)


@pytest.fixture
def fake_service():
    service = FakeOperatingRuleService()
    app.dependency_overrides[get_operating_rule_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_operating_rule_service, None)


# --- 권한 (NFR-005) ---------------------------------------------------------------


@pytest.mark.parametrize("call", [_get, _post])
async def test_without_cookie_is_401(fake_service, call):
    response = await call(role=None)

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHENTICATED"
    assert fake_service.calls == []


@pytest.mark.parametrize("call", [_get, _post])
@pytest.mark.parametrize("role", ["CLIENT_MEMBER", "CLIENT_LEAD", "OWNER"])
async def test_non_admin_is_403(fake_service, call, role):
    response = await call(role=role)

    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
    assert fake_service.calls == []


@pytest.mark.parametrize("call", [_get, _post])
@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (ForbiddenException(), 403, "FORBIDDEN"),  # 배정되지 않은 고객사
        (CompanyNotFoundException(), 404, "COMPANY_NOT_FOUND"),
        (CompanyInactiveException(), 409, "COMPANY_INACTIVE"),
    ],
)
async def test_service_rejection_is_returned_as_is(
    fake_service, call, error, status_code, code
):
    fake_service.error = error

    response = await call()

    assert response.status_code == status_code
    assert response.json()["code"] == code


# --- 조회 (API 4) -----------------------------------------------------------------


async def test_admin_gets_versions_in_camel_case(fake_service):
    response = await _get()

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"items"}
    item = body["items"][0]
    assert set(item) == {
        "operatingRuleSetId",
        "versionNo",
        "effectiveDate",
        "safetyStockDays",
        "salesBasisDays",
        "isPartialFulfillmentAllowed",
        "createdAt",
        "author",
        "isCurrent",
        "leadTimes",
    }
    assert item["effectiveDate"] == "2026-07-03"
    assert item["isPartialFulfillmentAllowed"] is None  # 정책 미정
    assert item["isCurrent"] is True
    # 요청자에게 보이지 않는 작성자: ID 만 있고 이름·역할은 null
    assert item["author"] == {
        "userId": str(AUTHOR_ID),
        "displayName": None,
        "roleCode": None,
    }
    assert item["leadTimes"] == [
        {
            "leadTimeType": "AIR_TRANSIT",
            "warehouse": {
                "warehouseId": str(WAREHOUSE_ID),
                "warehouseCode": "WH-KR-01",
                "regionCode": "KR",
            },
            "toRegionCode": "WEST",
            "days": 5,
            "dayBasis": "CALENDAR_DAY",
        }
    ]
    name, kwargs = fake_service.calls[0]
    assert name == "list"
    assert kwargs["company_id"] == COMPANY_ID
    assert kwargs["actor"].role_code == "ADMIN"


@pytest.mark.parametrize("params", [{}, {"companyId": "not-a-uuid"}])
async def test_get_without_valid_company_id_is_422(fake_service, params):
    response = await _get(params=params)

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert fake_service.calls == []


# --- 저장 (API 5) -----------------------------------------------------------------


async def test_admin_creates_new_version(fake_service):
    response = await _post()

    assert response.status_code == 201
    assert response.json() == {
        "operatingRuleSetId": str(RULE_SET_ID),
        "versionNo": 2,
    }
    name, kwargs = fake_service.calls[0]
    assert name == "create"
    assert kwargs["actor"].role_code == "ADMIN"
    assert kwargs["company_id"] == COMPANY_ID
    assert kwargs["effective_from"] == date(2026, 7, 3)
    assert (kwargs["safety_stock_days"], kwargs["sales_basis_days"]) == (14, 28)
    assert kwargs["allow_partial_fulfillment"] is False
    assert kwargs["lead_times"] == [
        NewLeadTime("AIR_TRANSIT", WAREHOUSE_ID, "WEST", 5, "CALENDAR_DAY"),
        NewLeadTime("OUTBOUND_HANDLING", WAREHOUSE_ID, None, 1, "CALENDAR_DAY"),
    ]


async def test_partial_fulfillment_is_optional_and_defaults_to_null(fake_service):
    body = _body()
    del body["isPartialFulfillmentAllowed"]

    response = await _post(body)

    assert response.status_code == 201
    assert fake_service.calls[0][1]["allow_partial_fulfillment"] is None


@pytest.mark.parametrize(
    "body",
    [
        {k: v for k, v in _body().items() if k != "companyId"},
        {k: v for k, v in _body().items() if k != "leadTimes"},
        _body(effectiveDate="20260703"),
        _body(effectiveDate="1782777600"),  # Unix 초를 날짜로 받지 않는다 (IsoDate)
        _body(safetyStockDays=0),
        _body(salesBasisDays=0),
        _body(salesBasisDays=-1),
        _body(leadTimes=[_lead_time(days=-1)]),
        _body(leadTimes=[_lead_time(leadTimeType="SEA_TRANSIT")]),
        _body(leadTimes=[_lead_time(dayBasis="WEEK")]),
        _body(leadTimes=[_lead_time(toRegionCode="MARS")]),
        _body(leadTimes=[_lead_time(warehouseId="not-a-uuid")]),
    ],
    ids=[
        "no-company",
        "no-lead-times",
        "date-format",
        "date-unix",
        "safety-zero",
        "basis-zero",
        "basis-negative",
        "days-negative",
        "unknown-type",
        "unknown-basis",
        "unknown-region",
        "warehouse-not-uuid",
    ],
)
async def test_invalid_body_is_422(fake_service, body):
    response = await _post(body)

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert fake_service.calls == []


async def test_invalid_lead_time_from_service_is_400(fake_service):
    fake_service.error = InvalidLeadTimeException("같은 키 중복")

    response = await _post()

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_LEAD_TIME"
