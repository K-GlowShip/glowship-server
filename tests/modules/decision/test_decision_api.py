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
from app.modules.decision.application.decision_list_service import (
    DecisionListItem,
    DecisionListResult,
)
from app.modules.decision.application.exception.decision_exceptions import (
    InvalidDecidedPeriodException,
)
from app.modules.decision.dependencies import get_decision_list_service

URL = "/api/v1/decisions"
COMPANY_ID = uuid.uuid7()
USER_ID = uuid.uuid7()
SHIPMENT_ID = uuid.uuid7()
SKU_ID = uuid.uuid7()
RUN_ID = uuid.uuid7()
OPTION_ID = uuid.uuid7()
DELAY_EVENT_ID = uuid.uuid7()
DECISION_ID = uuid.uuid7()
ITEM_KEYS = {
    "decisionType",
    "decisionId",
    "decidedAt",
    "shipment",
    "representativeSku",
    "responseOption",
    "decidedBy",
    "status",
    "run",
    "delayEventId",
}


def _access_cookie(role_code: str, company_id: uuid.UUID | None = None) -> dict:
    token = create_access_token(
        user_id=USER_ID,
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


def _item(**overrides) -> DecisionListItem:
    values = {
        "decision_type": "FINAL",
        "decision_id": DECISION_ID,
        "decided_at": datetime(2026, 1, 2, 4, tzinfo=UTC),
        "status": "FINAL",
        "shipment_id": SHIPMENT_ID,
        "shipment_code": "OCN-20251215",
        "representative_sku_id": SKU_ID,
        "representative_sku_code": "SKU-002",
        "representative_product_name": "A Glow Hydra Serum 30ml",
        "response_option_id": OPTION_ID,
        "option_type": "KR_AIR_REPLENISH",
        "user_id": USER_ID,
        "display_name": "A Beauty Company Lead",
        "role_code": "CLIENT_LEAD",
        "run_id": RUN_ID,
        "run_code": "AN-260102-001",
        "run_status": "FINALIZED",
        "run_result_class": "FORECAST_ONLY_READY",
        "run_failure_code": None,
        "delay_event_id": DELAY_EVENT_ID,
    }
    values.update(overrides)
    return DecisionListItem(**values)


class FakeDecisionListService:
    """서비스는 단위 테스트에서 검증하므로, 라우터 테스트에서는 받은 인자만 기록하고 고정 결과를 돌려준다."""

    def __init__(self):
        self.calls: list = []
        self.error: Exception | None = None

    async def get_page(self, tenant, **kwargs):
        self.calls.append((tenant, kwargs))
        if self.error is not None:
            raise self.error
        return DecisionListResult(
            items=[
                _item(),
                _item(
                    decision_type="DRAFT",
                    decision_id=uuid.uuid7(),
                    decided_at=datetime(2026, 1, 1, 2, tzinfo=UTC),
                    status="NEEDS_REANALYSIS",
                    representative_sku_id=None,
                    representative_sku_code=None,
                    representative_product_name=None,
                    response_option_id=None,
                    option_type=None,
                    display_name="A Beauty Company Member 1",
                    role_code="CLIENT_MEMBER",
                    run_code="AN-260101-003",
                    run_status="STALE",
                ),
            ],
            total_count=4,
            page=kwargs["page"],
        )


@pytest.fixture
def fake_service():
    service = FakeDecisionListService()
    session = FakeSession()

    async def _session():
        yield session

    app.dependency_overrides[get_decision_list_service] = lambda: service
    app.dependency_overrides[get_db] = _session
    yield service
    app.dependency_overrides.pop(get_decision_list_service, None)
    app.dependency_overrides.pop(get_db, None)


# --- 인증·권한 -----------------------------------------------------------------


async def test_쿠키가_없으면_401을_반환한다(fake_service):
    response = await _get()

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHENTICATED"
    assert fake_service.calls == []


@pytest.mark.parametrize("role_code", ["OWNER", "ADMIN"])
async def test_고객사_범위가_없는_역할은_403을_반환한다(fake_service, role_code):
    response = await _get(_access_cookie(role_code))

    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
    assert fake_service.calls == []


# --- 파라미터 검증 ---------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {"from": "2026-10-01"},  # 날짜만
        {"to": "2026-10-21T15:00:00"},  # 시간대 없음
        {"from": "1782777600"},  # Unix 초
        {"decisionType": "MEMO"},
        {"status": "APPROVED"},
        {"decidedBy": "not-a-uuid"},
        {"runId": "123"},
        {"page": 0},
        {"size": 101},
        {"q": "x" * 101},
    ],
)
async def test_형식이_잘못된_파라미터는_422를_반환한다(fake_service, params):
    response = await _get(_access_cookie("CLIENT_LEAD", COMPANY_ID), params)

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert fake_service.calls == []


async def test_서비스가_기간_오류를_던지면_422를_반환한다(fake_service):
    fake_service.error = InvalidDecidedPeriodException()

    response = await _get(
        _access_cookie("CLIENT_LEAD", COMPANY_ID),
        {"from": "2026-01-03T00:00:00Z", "to": "2026-01-02T00:00:00Z"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_필터를_서비스에_그대로_넘긴다(fake_service):
    decided_by = uuid.uuid7()
    run_id = uuid.uuid7()

    response = await _get(
        _access_cookie("CLIENT_MEMBER", COMPANY_ID),
        {
            "from": "2026-09-30T15:00:00Z",
            "to": "2026-10-21T15:00:00Z",
            "q": "OCN",
            "decisionType": "DRAFT",
            "status": "NEEDS_REANALYSIS",
            "decidedBy": str(decided_by),
            "runId": str(run_id),
            "page": 2,
            "size": 10,
        },
    )

    assert response.status_code == 200
    [(tenant, kwargs)] = fake_service.calls
    assert tenant.company_id == COMPANY_ID
    assert tenant.user_id == USER_ID
    assert kwargs["decided_from"] == datetime(2026, 9, 30, 15, tzinfo=UTC)
    assert kwargs["decided_to"] == datetime(2026, 10, 21, 15, tzinfo=UTC)
    assert kwargs["query"] == "OCN"
    assert kwargs["decision_type"] == "DRAFT"
    assert kwargs["status"] == "NEEDS_REANALYSIS"
    assert kwargs["decided_by"] == decided_by
    assert kwargs["run_id"] == run_id
    assert isinstance(kwargs["page"], PageParams)
    assert (kwargs["page"].page, kwargs["page"].size) == (2, 10)


async def test_필터를_생략하면_None_과_기본_페이지를_넘긴다(fake_service):
    response = await _get(_access_cookie("CLIENT_LEAD", COMPANY_ID))

    assert response.status_code == 200
    [(_, kwargs)] = fake_service.calls
    for key in ("decided_from", "decided_to", "query", "decision_type", "status"):
        assert kwargs[key] is None
    assert kwargs["decided_by"] is None and kwargs["run_id"] is None
    assert (kwargs["page"].page, kwargs["page"].size) == (1, 20)


# --- 응답 모양 -------------------------------------------------------------------


async def test_성공하면_공통_목록_모양과_camelCase_행을_반환한다(fake_service):
    response = await _get(_access_cookie("CLIENT_LEAD", COMPANY_ID), {"size": 10})

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"items", "page", "size", "totalCount", "totalPages"}
    assert (body["page"], body["size"], body["totalCount"], body["totalPages"]) == (
        1,
        10,
        4,
        1,
    )
    assert len(body["items"]) == 2

    final = body["items"][0]
    assert set(final) == ITEM_KEYS
    assert final["decisionType"] == "FINAL"
    assert final["decisionId"] == str(DECISION_ID)
    assert final["decidedAt"] == "2026-01-02T04:00:00Z"
    assert final["status"] == "FINAL"
    assert final["shipment"] == {
        "shipmentId": str(SHIPMENT_ID),
        "shipmentCode": "OCN-20251215",
    }
    assert final["representativeSku"] == {
        "skuId": str(SKU_ID),
        "skuCode": "SKU-002",
        "productName": "A Glow Hydra Serum 30ml",
    }
    assert final["responseOption"] == {
        "responseOptionId": str(OPTION_ID),
        "optionType": "KR_AIR_REPLENISH",
    }
    assert final["decidedBy"] == {
        "userId": str(USER_ID),
        "displayName": "A Beauty Company Lead",
        "roleCode": "CLIENT_LEAD",
    }
    assert final["run"] == {
        "runId": str(RUN_ID),
        "runCode": "AN-260102-001",
        "status": "FINALIZED",
        "resultClass": "FORECAST_ONLY_READY",
        "failureCode": None,
    }
    assert final["delayEventId"] == str(DELAY_EVENT_ID)
    # 선택 이유·의견 본문은 목록에 없다 (상세 API 몫)
    assert "reasonText" not in final and "opinionText" not in final


async def test_대표_상품과_대응안이_없으면_null_로_보낸다(fake_service):
    response = await _get(_access_cookie("CLIENT_LEAD", COMPANY_ID))

    draft = response.json()["items"][1]
    assert set(draft) == ITEM_KEYS  # 값이 없어도 필드를 빼지 않는다
    assert draft["decisionType"] == "DRAFT"
    assert draft["status"] == "NEEDS_REANALYSIS"
    assert draft["representativeSku"] is None
    assert draft["responseOption"] is None
    assert draft["decidedBy"]["roleCode"] == "CLIENT_MEMBER"
    assert draft["run"]["status"] == "STALE"
    assert draft["run"]["runCode"] == "AN-260101-003"


async def test_RLS_컨텍스트를_토큰의_사용자와_고객사로_설정한다(fake_service):
    session = FakeSession()

    async def _session():
        yield session

    app.dependency_overrides[get_db] = _session
    response = await _get(_access_cookie("CLIENT_LEAD", COMPANY_ID))

    assert response.status_code == 200
    [(sql, params)] = session.executed
    assert "set_config('app.company_id'" in sql
    assert params == {"user_id": str(USER_ID), "company_id": str(COMPANY_ID)}
