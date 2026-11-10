import uuid
from dataclasses import dataclass, field

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
from app.modules.decision.application.reanalysis_service import ReanalysisResult
from app.modules.decision.dependencies import get_reanalysis_service
from app.modules.delay_analysis.application.exception.analysis_run_query_exceptions import (
    AnalysisRunNotFoundException,
)

"""
    재분석 요청 API 라우터 테스트 (API 20 POST /analysis-runs/{runId}/reanalysis)
      - 서비스는 SQL 시험이 검증하므로 여기서는 역할(팀원·팀장), 본문 선택, 202 응답 모양, 오류 코드 전달을 본다.
"""

COMPANY_ID = uuid.uuid7()
USER_ID = uuid.uuid7()
RUN_ID = uuid.uuid7()
NEW_RUN_ID = uuid.uuid7()
URL = f"/api/v1/analysis-runs/{RUN_ID}/reanalysis"
_NO_BODY = object()


def _cookie(role_code: str) -> dict:
    token = create_access_token(
        user_id=USER_ID,
        role_code=role_code,
        company_id=None if role_code in ("ADMIN", "OWNER") else COMPANY_ID,
        config=get_settings().jwt_config,
    )
    return {ACCESS_TOKEN_COOKIE: token}


async def _post(body=_NO_BODY, role: str | None = "CLIENT_LEAD", url: str = URL):
    cookies = _cookie(role) if role else None
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", cookies=cookies
    ) as client:
        if body is _NO_BODY:
            return await client.post(url)
        return await client.post(url, json=body)


@dataclass
class FakeSession:
    executed: list = field(default_factory=list)

    async def execute(self, statement, params=None):
        self.executed.append((str(statement), params))


class FakeService:
    def __init__(self):
        self.calls: list[tuple] = []
        self.error: Exception | None = None

    async def request(self, tenant, **kwargs):
        self.calls.append((tenant, kwargs))
        if self.error is not None:
            raise self.error
        return ReanalysisResult(
            run_id=NEW_RUN_ID, version_no=3, parent_run_id=RUN_ID, status="DETECTED"
        )


@pytest.fixture
def fake_service():
    session = FakeSession()

    async def _get_db():
        yield session

    service = FakeService()
    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_reanalysis_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_reanalysis_service, None)


@pytest.mark.parametrize("role", ["CLIENT_MEMBER", "CLIENT_LEAD"])
async def test_client_gets_202_with_new_run(fake_service, role):
    response = await _post({"requestReason": "입고일 재확인"}, role=role)

    assert response.status_code == 202
    assert response.json() == {
        "runId": str(NEW_RUN_ID),
        "versionNo": 3,
        "parentRunId": str(RUN_ID),
        "status": "DETECTED",
    }
    [(tenant, kwargs)] = fake_service.calls
    assert (tenant.company_id, tenant.user_id) == (COMPANY_ID, USER_ID)
    assert kwargs == {"analysis_run_id": RUN_ID, "request_reason": "입고일 재확인"}


@pytest.mark.parametrize("body", [_NO_BODY, {}, {"requestReason": None}])
async def test_body_is_optional(fake_service, body):
    response = await _post(body)

    assert response.status_code == 202
    assert fake_service.calls[0][1]["request_reason"] is None


@pytest.mark.parametrize(
    ("reason", "saved"),
    [
        ("  입고일 재확인  ", "입고일 재확인"),  # 앞뒤 공백을 뺀다
        ("   ", None),  # 공백만이면 없는 것으로 본다(선택 항목)
        ("", None),
        ("가" * 600, "가" * 600),  # 600자까지
    ],
)
async def test_request_reason_is_trimmed_and_blank_becomes_null(
    fake_service, reason, saved
):
    response = await _post({"requestReason": reason})

    assert response.status_code == 202
    assert fake_service.calls[0][1]["request_reason"] == saved


async def test_request_reason_over_600_returns_422(fake_service):
    response = await _post({"requestReason": "가" * 601})

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert fake_service.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {"requestReason": 1},
        [],
        {"requestReason": "이유\x00"},  # NUL (이슈 #49)
        {
            "requestReason": "  \x00  "
        },  # 공백뿐이어도 NUL 이 있으면 null 로 바꾸지 않고 거부
    ],
)
async def test_invalid_body_returns_422(fake_service, body):
    response = await _post(body)

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert fake_service.calls == []


async def test_invalid_run_id_returns_422(fake_service):
    response = await _post(url="/api/v1/analysis-runs/not-a-uuid/reanalysis")

    assert response.status_code == 422
    assert fake_service.calls == []


async def test_without_cookie_returns_401(fake_service):
    assert (await _post(role=None)).status_code == 401
    assert fake_service.calls == []


@pytest.mark.parametrize("role", ["ADMIN", "OWNER"])
async def test_non_client_returns_403(fake_service, role):
    response = await _post(role=role)

    assert response.status_code == 403
    assert fake_service.calls == []


@pytest.mark.parametrize(
    "error",
    [
        DecisionException(code)
        for code in (
            DecisionErrorCode.EPISODE_CLOSED,
            DecisionErrorCode.NOT_CURRENT_RUN,
            DecisionErrorCode.RUN_IN_PROGRESS,
            DecisionErrorCode.RUN_FINALIZED,
            DecisionErrorCode.REANALYSIS_NOT_ALLOWED,
        )
    ]
    + [AnalysisRunNotFoundException()],
    ids=lambda error: error.error_code.code,
)
async def test_service_rejection_is_returned_as_is(fake_service, error):
    fake_service.error = error

    response = await _post()

    assert response.status_code == error.error_code.status_code
    assert response.json()["code"] == error.error_code.code


# --- 분석 자동 시작 연결 (GLOW-128) --------------------------------------------------------------


class RecordingRunner:
    def __init__(self):
        self.started: list[tuple] = []

    def start(self, company_id, run_id):
        self.started.append((company_id, run_id))
        return True


@pytest.fixture
def runner():
    from app.modules.delay_analysis.application.run_start.app_runner import (
        get_analysis_runner,
    )

    recording = RecordingRunner()
    app.dependency_overrides[get_analysis_runner] = lambda: recording
    yield recording
    app.dependency_overrides.pop(get_analysis_runner, None)


async def test_new_run_is_handed_to_runner_after_service(fake_service, runner):
    response = await _post()

    assert response.status_code == 202
    assert runner.started == [(COMPANY_ID, NEW_RUN_ID)]


async def test_rejected_request_starts_nothing(fake_service, runner):
    fake_service.error = DecisionException(DecisionErrorCode.RUN_IN_PROGRESS)

    response = await _post()

    assert response.status_code == 409
    assert runner.started == []
