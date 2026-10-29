from datetime import timedelta
from typing import Literal

from starlette.responses import Response

"""
    인증 쿠키 규칙
      - 이름·Path·SameSite 는 발급(로그인)과 재발급·삭제(로그아웃)가 같은 값을 써야 하므로 이곳에만 둔다.
      - refresh 쿠키는 Path 를 재발급 엔드포인트로 좁혀 다른 요청에는 실리지 않게 한다.
"""

ACCESS_TOKEN_COOKIE = "access_token"
REFRESH_TOKEN_COOKIE = "refresh_token"

ACCESS_TOKEN_COOKIE_PATH = "/"
REFRESH_TOKEN_COOKIE_PATH = "/api/v1/auth/refresh"

# 프론트와 API 가 다른 등록 도메인(site)에 배포되면 "none" 으로 바꿔야 쿠키가 전송된다.
COOKIE_SAMESITE: Literal["lax", "strict", "none"] = "lax"


def set_auth_cookies(
    response: Response,
    *,
    access_token: str,
    refresh_token: str,
    access_max_age: timedelta,
    refresh_max_age: timedelta,
    secure: bool,
) -> None:
    """
    access / refresh 토큰을 HttpOnly 쿠키로 응답에 싣는다.

    Args:
        response: 쿠키를 붙일 응답 객체
        access_token: API 접근용 토큰
        refresh_token: 재발급용 토큰
        access_max_age: access 쿠키 수명 (토큰 만료와 같은 값을 넘긴다)
        refresh_max_age: refresh 쿠키 수명 (토큰 만료와 같은 값을 넘긴다)
        secure: HTTPS 에서만 전송할지 여부 (Settings.cookie_secure_enabled)
    """
    response.set_cookie(
        key=ACCESS_TOKEN_COOKIE,
        value=access_token,
        max_age=int(access_max_age.total_seconds()),
        path=ACCESS_TOKEN_COOKIE_PATH,
        secure=secure,
        httponly=True,
        samesite=COOKIE_SAMESITE,
    )
    response.set_cookie(
        key=REFRESH_TOKEN_COOKIE,
        value=refresh_token,
        max_age=int(refresh_max_age.total_seconds()),
        path=REFRESH_TOKEN_COOKIE_PATH,
        secure=secure,
        httponly=True,
        samesite=COOKIE_SAMESITE,
    )
