import uuid
from dataclasses import replace
from datetime import timedelta
from http.cookies import SimpleCookie

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.core.exceptions import UnauthenticatedException
from app.core.security.cookies import (
    ACCESS_TOKEN_COOKIE,
    ACCESS_TOKEN_COOKIE_PATH,
    REFRESH_TOKEN_COOKIE,
)
from app.core.security.jwt import create_access_token, create_refresh_token
from app.main import app
from app.modules.user.application.exception.user_exceptions import (
    AccountDisabledException,
)
from app.modules.user.dependencies import get_auth_service

"""
    POST /api/v1/auth/refresh 라우터 (API 27, GLOW-152)
      - 서비스는 test_auth_service_refresh.py 에서 검증하므로 여기서는 가짜 서비스로 바꾸고
        쿠키 읽기·응답 쿠키·예외 변환만 본다.
"""

URL = "/api/v1/auth/refresh"
USER_ID = uuid.uuid7()
NEW_ACCESS_TOKEN = "new-access-token"


class FakeAuthService:
    def __init__(self):
        self.calls: list[str] = []
        self.error: Exception | None = None

    async def refresh_access_token(self, refresh_token: str) -> str:
        self.calls.append(refresh_token)
        if self.error is not None:
            raise self.error
        return NEW_ACCESS_TOKEN


@pytest.fixture
def fake_service():
    service = FakeAuthService()
    app.dependency_overrides[get_auth_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_auth_service, None)


async def _post(cookies: dict | None = None, headers: dict | None = None):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies=cookies,
        headers=headers,
    ) as client:
        return await client.post(URL)


def _set_cookies(response) -> dict[str, SimpleCookie]:
    parsed: dict[str, SimpleCookie] = {}
    for header in response.headers.get_list("set-cookie"):
        cookie = SimpleCookie()
        cookie.load(header)
        for name in cookie:
            parsed[name] = cookie
    return parsed


def _refresh_cookie() -> dict:
    token = create_refresh_token(user_id=USER_ID, config=get_settings().jwt_config)
    return {REFRESH_TOKEN_COOKIE: token}


async def test_refresh_쿠키가_없으면_401이고_서비스를_부르지_않는다(fake_service):
    response = await _post()

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHENTICATED"
    assert fake_service.calls == []
    assert response.headers.get_list("set-cookie") == []


async def test_access_쿠키만_있으면_401이다(fake_service):
    """access 쿠키는 Path 가 / 라 이 요청에도 실리지만, 재발급은 refresh 쿠키만 본다."""
    token = create_access_token(
        user_id=USER_ID,
        role_code="CLIENT_LEAD",
        company_id=uuid.uuid7(),
        config=get_settings().jwt_config,
    )

    response = await _post({ACCESS_TOKEN_COOKIE: token})

    assert response.status_code == 401
    assert fake_service.calls == []


async def test_성공하면_204와_access_쿠키만_새로_내린다(fake_service):
    cookies = _refresh_cookie()

    response = await _post(cookies)

    assert response.status_code == 204
    assert response.content == b""
    assert fake_service.calls == [cookies[REFRESH_TOKEN_COOKIE]]

    set_cookies = _set_cookies(response)
    assert set(set_cookies) == {ACCESS_TOKEN_COOKIE}  # refresh 쿠키는 회전하지 않는다
    access = set_cookies[ACCESS_TOKEN_COOKIE][ACCESS_TOKEN_COOKIE]
    settings = get_settings()
    assert access.value == NEW_ACCESS_TOKEN
    assert access["path"] == ACCESS_TOKEN_COOKIE_PATH
    assert access["max-age"] == str(
        int(settings.jwt_config.access_token_expires.total_seconds())
    )
    assert access["httponly"]
    assert access["samesite"].lower() == "lax"
    assert bool(access["secure"]) == settings.cookie_secure_enabled


async def test_서비스가_401을_던지면_401이고_쿠키를_내리지_않는다(fake_service):
    fake_service.error = UnauthenticatedException()

    response = await _post(_refresh_cookie())

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHENTICATED"
    assert response.headers.get_list("set-cookie") == []


async def test_서비스가_비활성_계정을_던지면_403이다(fake_service):
    fake_service.error = AccountDisabledException()

    response = await _post(_refresh_cookie())

    assert response.status_code == 403
    assert response.json()["code"] == "ACCOUNT_DISABLED"
    assert response.headers.get_list("set-cookie") == []


async def test_허용되지_않은_Origin_이면_403이고_서비스를_부르지_않는다(fake_service):
    response = await _post(
        _refresh_cookie(), headers={"Origin": "https://evil.example"}
    )

    assert response.status_code == 403
    assert response.json()["code"] == "INVALID_ORIGIN"
    assert fake_service.calls == []


async def test_허용된_Origin_이면_204다(fake_service):
    [origin, *_] = get_settings().cors_origins

    response = await _post(_refresh_cookie(), headers={"Origin": origin})

    assert response.status_code == 204


async def test_실제_서비스_경로에서_만료된_refresh_토큰은_401이다():
    """dependency_overrides 없이 실제 서비스가 토큰을 먼저 검증해 DB 에 가기 전에 거절하는지 본다."""
    config = get_settings().jwt_config
    expired = replace(config, refresh_token_expires=timedelta(seconds=-1))
    token = create_refresh_token(user_id=USER_ID, config=expired)

    response = await _post({REFRESH_TOKEN_COOKIE: token})

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHENTICATED"
