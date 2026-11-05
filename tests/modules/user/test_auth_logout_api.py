import uuid
from http.cookies import SimpleCookie

from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.core.security.cookies import (
    ACCESS_TOKEN_COOKIE,
    ACCESS_TOKEN_COOKIE_PATH,
    REFRESH_TOKEN_COOKIE,
    REFRESH_TOKEN_COOKIE_PATH,
)
from app.core.security.jwt import create_access_token
from app.main import app

URL = "/api/v1/auth/logout"


async def _post(cookies: dict | None = None, headers: dict | None = None):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies=cookies,
        headers=headers,
    ) as client:
        return await client.post(URL)


def _set_cookies(response) -> dict[str, SimpleCookie]:
    """Set-Cookie 헤더를 쿠키 이름별로 파싱한다. 삭제 쿠키는 Max-Age=0 과 과거 expires 를 갖는다."""
    parsed: dict[str, SimpleCookie] = {}
    for header in response.headers.get_list("set-cookie"):
        cookie = SimpleCookie()
        cookie.load(header)
        for name in cookie:
            parsed[name] = cookie
    return parsed


def _access_cookie() -> dict:
    token = create_access_token(
        user_id=uuid.uuid7(),
        role_code="CLIENT_LEAD",
        company_id=uuid.uuid7(),
        config=get_settings().jwt_config,
    )
    return {ACCESS_TOKEN_COOKIE: token}


async def test_쿠키가_없어도_204를_반환한다():
    response = await _post()

    assert response.status_code == 204
    assert response.content == b""


async def test_유효한_쿠키로_호출하면_204와_두_쿠키의_삭제_지시를_반환한다():
    response = await _post(_access_cookie())

    assert response.status_code == 204
    cookies = _set_cookies(response)
    assert set(cookies) == {ACCESS_TOKEN_COOKIE, REFRESH_TOKEN_COOKIE}

    access = cookies[ACCESS_TOKEN_COOKIE][ACCESS_TOKEN_COOKIE]
    refresh = cookies[REFRESH_TOKEN_COOKIE][REFRESH_TOKEN_COOKIE]
    for morsel in (access, refresh):
        assert morsel.value == ""
        assert morsel["max-age"] == "0"
        assert morsel["httponly"]
        assert morsel["samesite"].lower() == "lax"
    # 발급과 같은 Path 여야 브라우저가 기존 쿠키를 덮어쓴다
    assert access["path"] == ACCESS_TOKEN_COOKIE_PATH
    assert refresh["path"] == REFRESH_TOKEN_COOKIE_PATH


async def test_Secure_속성은_설정을_따른다():
    response = await _post()

    cookies = _set_cookies(response)
    expected = get_settings().cookie_secure_enabled
    for name in (ACCESS_TOKEN_COOKIE, REFRESH_TOKEN_COOKIE):
        assert bool(cookies[name][name]["secure"]) == expected


async def test_만료된_토큰이나_잘못된_값이어도_204다():
    response = await _post({ACCESS_TOKEN_COOKIE: "not-a-jwt"})

    assert response.status_code == 204
    assert set(_set_cookies(response)) == {ACCESS_TOKEN_COOKIE, REFRESH_TOKEN_COOKIE}


async def test_허용되지_않은_Origin_이면_403이고_쿠키를_지우지_않는다():
    response = await _post(headers={"Origin": "https://evil.example"})

    assert response.status_code == 403
    assert response.json()["code"] == "INVALID_ORIGIN"
    assert response.headers.get_list("set-cookie") == []


async def test_허용된_Origin_이면_204다():
    [origin, *_] = get_settings().cors_origins

    response = await _post(headers={"Origin": origin})

    assert response.status_code == 204
