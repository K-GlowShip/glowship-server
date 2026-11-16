import base64
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.core.exceptions import ForbiddenException
from app.core.security.cookies import ACCESS_TOKEN_COOKIE
from app.core.security.jwt import create_access_token
from app.main import app
from app.modules.user.application.exception.user_exceptions import (
    InvalidPasswordFormatException,
    UserNotFoundException,
)
from app.modules.user.dependencies import get_client_account_service

ADMIN_ID = uuid.uuid7()
TARGET_USER_ID = uuid.uuid7()
COMPANY_ID = uuid.uuid7()
URL = f"/api/v1/admin/users/{TARGET_USER_ID}/password"
ENCODED_PASSWORD = base64.b64encode(b"newpass123").decode()
BODY = {"password": ENCODED_PASSWORD}


def _access_cookie(role_code: str, company_id: uuid.UUID | None = None) -> dict:
    token = create_access_token(
        user_id=ADMIN_ID,
        role_code=role_code,
        company_id=company_id,
        config=get_settings().jwt_config,
    )
    return {ACCESS_TOKEN_COOKIE: token}


async def _post(body: dict, cookies: dict | None = None, url: str = URL):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", cookies=cookies
    ) as client:
        return await client.post(url, json=body)


class FakeClientAccountService:
    """서비스는 단위 테스트에서 검증하므로, 라우터 테스트에서는 호출 인자만 기록한다."""

    def __init__(self):
        self.calls: list[dict] = []
        self.error: Exception | None = None

    async def reset_client_user_password(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error


@pytest.fixture
def fake_service():
    service = FakeClientAccountService()
    app.dependency_overrides[get_client_account_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_client_account_service, None)


async def test_쿠키가_없으면_401을_반환한다(fake_service):
    response = await _post(BODY)

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHENTICATED"
    assert fake_service.calls == []


@pytest.mark.parametrize("role_code", ["OWNER", "CLIENT_LEAD", "CLIENT_MEMBER"])
async def test_ADMIN이_아니면_403을_반환한다(fake_service, role_code):
    company_id = None if role_code == "OWNER" else COMPANY_ID

    response = await _post(BODY, cookies=_access_cookie(role_code, company_id))

    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
    assert fake_service.calls == []


async def test_성공하면_204와_빈_본문을_반환한다(fake_service):
    response = await _post(BODY, cookies=_access_cookie("ADMIN"))

    assert response.status_code == 204
    assert response.content == b""
    # 기존 세션을 건드리지 않는다 (명세 D26). 쿠키를 지우거나 새로 심지 않는다.
    assert "set-cookie" not in response.headers

    [call] = fake_service.calls
    assert call["actor"].user_id == ADMIN_ID
    assert call["user_id"] == TARGET_USER_ID
    assert call["encoded_password"] == ENCODED_PASSWORD


async def test_password_가_없으면_422를_반환한다(fake_service):
    response = await _post({}, cookies=_access_cookie("ADMIN"))

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert fake_service.calls == []


async def test_userId_가_UUID_가_아니면_422를_반환한다(fake_service):
    response = await _post(
        BODY,
        cookies=_access_cookie("ADMIN"),
        url="/api/v1/admin/users/not-a-uuid/password",
    )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert fake_service.calls == []


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (InvalidPasswordFormatException(), 400, "INVALID_PASSWORD_FORMAT"),
        (ForbiddenException(), 403, "FORBIDDEN"),
        (UserNotFoundException(), 404, "USER_NOT_FOUND"),
    ],
)
async def test_서비스_예외를_오류_응답으로_바꾼다(
    fake_service, error, status_code, code
):
    fake_service.error = error

    response = await _post(BODY, cookies=_access_cookie("ADMIN"))

    assert response.status_code == status_code
    assert response.json()["code"] == code
