from datetime import timedelta
from typing import Literal

from starlette.responses import Response

"""
    인증 쿠키 규칙
      - 이름·Path·SameSite 는 발급(로그인)과 재발급·삭제(로그아웃)가 같은 값을 써야 하므로 이곳에만 둔다.
        브라우저는 이름·도메인·Path 가 같은 쿠키만 덮어쓰므로, 삭제 쿠키의 Path 가 발급과 다르면 지워지지 않는다.
      - refresh 쿠키는 Path 를 재발급 엔드포인트로 좁혀 다른 요청에는 실리지 않게 한다.
      - 재발급(POST /auth/refresh)은 access 쿠키만 새로 내린다(set_access_cookie). refresh 토큰은 회전하지 않아
        로그인 뒤 7일이 지나면 다시 로그인한다(명세 D4 "쿠키 수명 고정", GLOW-152).
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
    set_access_cookie(
        response, access_token=access_token, max_age=access_max_age, secure=secure
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


def set_access_cookie(
    response: Response,
    *,
    access_token: str,
    max_age: timedelta,
    secure: bool,
) -> None:
    """
    access 토큰만 HttpOnly 쿠키로 응답에 싣는다 (로그인의 access 부분, 재발급).

    로그인(set_auth_cookies)과 같은 이름·Path·속성을 써야 브라우저가 기존 access 쿠키를 덮어쓴다.

    Args:
        response: 쿠키를 붙일 응답 객체
        access_token: API 접근용 토큰
        max_age: access 쿠키 수명 (토큰 만료와 같은 값을 넘긴다)
        secure: HTTPS 에서만 전송할지 여부 (Settings.cookie_secure_enabled)
    """
    response.set_cookie(
        key=ACCESS_TOKEN_COOKIE,
        value=access_token,
        max_age=int(max_age.total_seconds()),
        path=ACCESS_TOKEN_COOKIE_PATH,
        secure=secure,
        httponly=True,
        samesite=COOKIE_SAMESITE,
    )


def clear_auth_cookies(response: Response, *, secure: bool) -> None:
    """
    access / refresh 쿠키를 만료시켜 브라우저가 지우게 한다 (로그아웃).

    set_auth_cookies 와 같은 이름·Path·SameSite·HttpOnly 로 Max-Age=0 쿠키를 내린다.
    서버는 토큰 값을 보거나 기록하지 않으므로, 로그아웃 요청에 refresh 쿠키가 실려 오지 않아도
    (Path 가 재발급 엔드포인트로 좁혀져 있음) 삭제 지시는 내릴 수 있다.

    Args:
        response: 삭제 쿠키를 붙일 응답 객체
        secure: 발급 때와 같은 Secure 여부 (Settings.cookie_secure_enabled)
    """
    response.delete_cookie(
        key=ACCESS_TOKEN_COOKIE,
        path=ACCESS_TOKEN_COOKIE_PATH,
        secure=secure,
        httponly=True,
        samesite=COOKIE_SAMESITE,
    )
    response.delete_cookie(
        key=REFRESH_TOKEN_COOKIE,
        path=REFRESH_TOKEN_COOKIE_PATH,
        secure=secure,
        httponly=True,
        samesite=COOKIE_SAMESITE,
    )
