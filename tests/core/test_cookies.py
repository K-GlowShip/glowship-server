from datetime import timedelta
from http.cookies import SimpleCookie

import pytest
from starlette.responses import Response

from app.core.security.cookies import (
    ACCESS_TOKEN_COOKIE,
    REFRESH_TOKEN_COOKIE,
    clear_auth_cookies,
    set_auth_cookies,
)

"""
    발급(set_auth_cookies)과 삭제(clear_auth_cookies)가 같은 이름·Path·속성을 쓰는지 쌍으로 대조한다.
    브라우저는 이름·도메인·Path 가 같은 쿠키만 덮어쓰므로, 한쪽 상수만 바꾸면 로그아웃이 쿠키를 지우지 못한다.
"""


def _cookies(response: Response) -> dict[str, SimpleCookie]:
    parsed: dict[str, SimpleCookie] = {}
    for key, value in response.raw_headers:
        if key == b"set-cookie":
            cookie = SimpleCookie()
            cookie.load(value.decode())
            for name in cookie:
                parsed[name] = cookie
    return parsed


@pytest.mark.parametrize("secure", [True, False])
def test_발급과_삭제_쿠키의_이름_Path_속성이_같다(secure):
    issued = Response()
    set_auth_cookies(
        issued,
        access_token="access",
        refresh_token="refresh",
        access_max_age=timedelta(minutes=30),
        refresh_max_age=timedelta(days=7),
        secure=secure,
    )
    cleared = Response()
    clear_auth_cookies(cleared, secure=secure)

    issued_cookies, cleared_cookies = _cookies(issued), _cookies(cleared)
    expected_names = {ACCESS_TOKEN_COOKIE, REFRESH_TOKEN_COOKIE}
    assert set(issued_cookies) == expected_names
    assert set(cleared_cookies) == expected_names
    for name in (ACCESS_TOKEN_COOKIE, REFRESH_TOKEN_COOKIE):
        a, b = issued_cookies[name][name], cleared_cookies[name][name]
        assert a["path"] == b["path"]
        assert a["samesite"] == b["samesite"]
        assert bool(a["httponly"]) == bool(b["httponly"]) is True
        assert bool(a["secure"]) == bool(b["secure"]) == secure


def test_삭제_쿠키는_빈_값과_Max_Age_0_이다():
    response = Response()
    clear_auth_cookies(response, secure=False)

    for name, cookie in _cookies(response).items():
        assert cookie[name].value == ""
        assert cookie[name]["max-age"] == "0"
        # 과거 시각 (Starlette delete_cookie 가 expires=0 으로 넣음)
        assert cookie[name]["expires"]
