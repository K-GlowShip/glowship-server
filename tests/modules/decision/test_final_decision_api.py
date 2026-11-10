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
from app.modules.decision.application.final_decision_service import (
    FinalDecisionResult,
)
from app.modules.decision.dependencies import get_final_decision_service
from app.modules.delay_analysis.application.exception.analysis_run_query_exceptions import (
    AnalysisRunNotFoundException,
)

"""
    최종 선택 API 라우터 테스트 (API 19 POST /analysis-runs/{runId}/final-decision)
      - 서비스는 SQL·대역 시험이 검증하므로 여기서는 팀장 전용, Idempotency-Key 헤더, reasonText 1~600자(C25),
        201 응답 모양, 오류 코드 전달을 본다.
"""

COMPANY_ID = uuid.uuid7()
USER_ID = uuid.uuid7()
RUN_ID = uuid.uuid7()
OPTION_ID = uuid.uuid7()
DECISION_ID = uuid.uuid7()
DECIDED_AT = datetime(2026, 7, 2, 0, 3, tzinfo=UTC)
URL = f"/api/v1/analysis-runs/{RUN_ID}/final-decision"
BODY = {"responseOptionId": str(OPTION_ID), "reasonText": "  재고 영향이 가장 적음  "}


def _cookie(role_code: str) -> dict:
    token = create_access_token(
        user_id=USER_ID,
        role_code=role_code,
        company_id=None if role_code in ("ADMIN", "OWNER") else COMPANY_ID,
        config=get_settings().jwt_config,
    )
    return {ACCESS_TOKEN_COOKIE: token}


async def _post(body=BODY, role="CLIENT_LEAD", key: str | None = "key-1", url=URL):
    cookies = _cookie(role) if role else None
    headers = {"Idempotency-Key": key} if key is not None else {}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", cookies=cookies
    ) as client:
        return await client.post(url, json=body, headers=headers)


@dataclass
class FakeSession:
    executed: list = field(default_factory=list)

    async def execute(self, statement, params=None):
        self.executed.append((str(statement), params))


class FakeService:
    def __init__(self):
        self.calls: list[tuple] = []
        self.error: Exception | None = None

    async def decide(self, tenant, **kwargs):
        self.calls.append((tenant, kwargs))
        if self.error is not None:
            raise self.error
        return FinalDecisionResult(
            final_decision_id=DECISION_ID,
            decided_at=DECIDED_AT,
            checked_seq=12,
            status="FINALIZED",
            replayed=False,
        )


@pytest.fixture
def fake_service():
    session = FakeSession()

    async def _get_db():
        yield session

    service = FakeService()
    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_final_decision_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_final_decision_service, None)


async def test_lead_gets_201_with_decision(fake_service):
    response = await _post()

    assert response.status_code == 201
    assert response.json() == {
        "finalDecisionId": str(DECISION_ID),
        "decidedAt": "2026-07-02T00:03:00Z",
        "checkedSeq": 12,
        "status": "FINALIZED",
    }
    [(tenant, kwargs)] = fake_service.calls
    assert (tenant.company_id, tenant.user_id) == (COMPANY_ID, USER_ID)
    # 이유는 앞뒤 공백을 뺀 값 (C25), 키도 앞뒤 공백을 뺌
    assert kwargs == {
        "analysis_run_id": RUN_ID,
        "response_option_id": OPTION_ID,
        "reason_text": "재고 영향이 가장 적음",
        "idempotency_key": "key-1",
    }


@pytest.mark.parametrize("key", [None, "", "   "])
async def test_missing_idempotency_key_is_400(fake_service, key):
    response = await _post(key=key)

    assert response.status_code == 400
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert fake_service.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {"responseOptionId": str(OPTION_ID)},
        {"responseOptionId": str(OPTION_ID), "reasonText": "   "},
        {"responseOptionId": str(OPTION_ID), "reasonText": "가" * 601},
        {
            "responseOptionId": str(OPTION_ID),
            "reasonText": "이유\x00",
        },  # NUL (이슈 #49)
        {"reasonText": "이유"},
        {"responseOptionId": "not-a-uuid", "reasonText": "이유"},
    ],
)
async def test_invalid_body_is_422(fake_service, body):
    response = await _post(body)

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert fake_service.calls == []


async def test_reason_of_600_chars_after_strip_is_accepted(fake_service):
    response = await _post(
        {"responseOptionId": str(OPTION_ID), "reasonText": " " + "가" * 600 + " "}
    )

    assert response.status_code == 201
    assert fake_service.calls[0][1]["reason_text"] == "가" * 600


async def test_without_cookie_is_401(fake_service):
    assert (await _post(role=None)).status_code == 401
    assert fake_service.calls == []


@pytest.mark.parametrize("role", ["CLIENT_MEMBER", "ADMIN", "OWNER"])
async def test_non_lead_is_403(fake_service, role):
    response = await _post(role=role)

    assert response.status_code == 403
    assert fake_service.calls == []


@pytest.mark.parametrize(
    "error",
    [
        DecisionException(code)
        for code in (
            DecisionErrorCode.EPISODE_CLOSED,
            DecisionErrorCode.EXPLANATION_NOT_READY,
            DecisionErrorCode.ANALYSIS_STALE,
            DecisionErrorCode.ANALYSIS_STALE_TIME_ELAPSED,
            DecisionErrorCode.FINAL_DECISION_EXISTS,
            DecisionErrorCode.NOT_CURRENT_RUN,
            DecisionErrorCode.RUN_NOT_READY,
            DecisionErrorCode.OPERATING_RULE_CHANGED,
            DecisionErrorCode.IDEMPOTENCY_KEY_REUSED,
            DecisionErrorCode.OPTION_NOT_IN_RUN,
            DecisionErrorCode.OPTION_NOT_FEASIBLE,
        )
    ]
    + [AnalysisRunNotFoundException()],
    ids=lambda error: error.error_code.name,
)
async def test_service_rejection_is_returned_as_is(fake_service, error):
    fake_service.error = error

    response = await _post()

    assert response.status_code == error.error_code.status_code
    assert response.json()["code"] == error.error_code.code
