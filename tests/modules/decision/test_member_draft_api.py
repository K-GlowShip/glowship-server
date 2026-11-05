import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security.cookies import ACCESS_TOKEN_COOKIE
from app.core.security.jwt import create_access_token
from app.main import app
from app.modules.decision.application.exception.decision_exceptions import (
    DecisionErrorCode,
    DecisionException,
)
from app.modules.decision.application.member_draft_service import SavedMemberDraft
from app.modules.decision.dependencies import get_member_draft_service
from app.modules.delay_analysis.application.exception.analysis_run_query_exceptions import (
    AnalysisRunNotFoundException,
)

"""
    의견·선택 초안 저장 API 라우터 테스트 (API 18 POST /analysis-runs/{runId}/drafts)
      - 서비스는 단위 테스트에서 검증하므로 여기서는 인증·역할, 입력 검증(C25), 응답 모양, 오류 코드 전달을 본다.
"""

COMPANY_ID = uuid.uuid7()
USER_ID = uuid.uuid7()
RUN_ID = uuid.uuid7()
OPTION_ID = uuid.uuid7()
DRAFT_ID = uuid.uuid7()
CREATED_AT = datetime(2026, 7, 2, 1, 30, tzinfo=UTC)
URL = f"/api/v1/analysis-runs/{RUN_ID}/drafts"


def _cookie(role_code: str) -> dict:
    token = create_access_token(
        user_id=USER_ID,
        role_code=role_code,
        company_id=None if role_code in ("ADMIN", "OWNER") else COMPANY_ID,
        config=get_settings().jwt_config,
    )
    return {ACCESS_TOKEN_COOKIE: token}


async def _post(body, role: str | None = "CLIENT_MEMBER", url: str = URL):
    cookies = _cookie(role) if role else None
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", cookies=cookies
    ) as client:
        return await client.post(url, json=body)


@dataclass
class FakeSession:
    """DB 없이 실행한다. get_tenant_context 의 RLS 컨텍스트 SQL 을 기록만 한다."""

    executed: list = field(default_factory=list)

    async def execute(self, statement, params=None):
        self.executed.append((str(statement), params))


class FakeService:
    def __init__(self):
        self.calls: list[tuple] = []
        self.error: Exception | None = None

    async def save_draft(self, tenant, **kwargs):
        self.calls.append((tenant, kwargs))
        if self.error is not None:
            raise self.error
        return SavedMemberDraft(member_draft_id=DRAFT_ID, created_at=CREATED_AT)


@pytest.fixture
def fake_service():
    session = FakeSession()

    async def _get_db():
        yield session

    service = FakeService()
    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_member_draft_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_member_draft_service, None)


# --- 저장 ------------------------------------------------------------------------


async def test_member_saves_option_and_opinion(fake_service):
    response = await _post(
        {"responseOptionId": str(OPTION_ID), "opinionText": "  항공 전환 검토  "}
    )

    assert response.status_code == 201
    assert response.json() == {
        "memberDraftId": str(DRAFT_ID),
        "createdAt": "2026-07-02T01:30:00Z",
    }
    [(tenant, kwargs)] = fake_service.calls
    assert (tenant.company_id, tenant.user_id) == (COMPANY_ID, USER_ID)
    # 의견은 앞뒤 공백을 뺀 값을 넘긴다 (C25)
    assert kwargs == {
        "analysis_run_id": RUN_ID,
        "response_option_id": OPTION_ID,
        "opinion_text": "항공 전환 검토",
    }


@pytest.mark.parametrize(
    ("body", "option", "opinion"),
    [
        ({"responseOptionId": str(OPTION_ID)}, OPTION_ID, None),
        ({"opinionText": "의견만"}, None, "의견만"),
        ({"opinionText": "가" * 600}, None, "가" * 600),
        # 앞뒤 공백은 길이에 넣지 않는다
        ({"opinionText": " " + "가" * 600 + "\n"}, None, "가" * 600),
    ],
)
async def test_option_or_opinion_alone_is_enough(fake_service, body, option, opinion):
    response = await _post(body)

    assert response.status_code == 201
    [(_, kwargs)] = fake_service.calls
    assert (kwargs["response_option_id"], kwargs["opinion_text"]) == (option, opinion)


# --- 입력 검증 (C25, 현재 구현 422) ---------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"responseOptionId": None, "opinionText": None},
        {"opinionText": "   "},
        {"opinionText": ""},
        {"responseOptionId": str(OPTION_ID), "opinionText": " \t\n"},
        {"opinionText": "가" * 601},
        {"responseOptionId": "not-a-uuid"},
    ],
)
async def test_invalid_body_returns_422_validation_error(fake_service, body):
    response = await _post(body)

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert fake_service.calls == []


async def test_invalid_run_id_returns_422(fake_service):
    response = await _post(
        {"opinionText": "의견"}, url="/api/v1/analysis-runs/not-a-uuid/drafts"
    )

    assert response.status_code == 422
    assert fake_service.calls == []


# --- 인증·역할 (D17: 팀원만) ---------------------------------------------------------


async def test_without_cookie_returns_401(fake_service):
    response = await _post({"opinionText": "의견"}, role=None)

    assert response.status_code == 401
    assert fake_service.calls == []


@pytest.mark.parametrize("role", ["CLIENT_LEAD", "ADMIN", "OWNER"])
async def test_non_member_returns_403(fake_service, role):
    response = await _post({"opinionText": "의견"}, role=role)

    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
    assert fake_service.calls == []


# --- 서비스 거부 전달 (명세 4.6 대응표, D22) -------------------------------------------


@pytest.mark.parametrize(
    "error",
    [DecisionException(code) for code in DecisionErrorCode]
    + [AnalysisRunNotFoundException()],
    ids=lambda error: error.error_code.code,
)
async def test_service_rejection_is_returned_as_is(fake_service, error):
    fake_service.error = error

    response = await _post({"responseOptionId": str(OPTION_ID)})

    assert response.status_code == error.error_code.status_code
    assert response.json()["code"] == error.error_code.code
