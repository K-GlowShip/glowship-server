from fastapi import APIRouter, Depends
from fastapi.security import APIKeyCookie
from starlette import status
from starlette.responses import Response

from app.core.config import Settings, get_settings
from app.core.exceptions import ErrorResponse, UnauthenticatedException
from app.core.security.cookies import (
    REFRESH_TOKEN_COOKIE,
    clear_auth_cookies,
    set_access_cookie,
    set_auth_cookies,
)
from app.modules.user.application.auth_service import AuthService
from app.modules.user.dependencies import get_auth_service
from app.modules.user.presentation.auth_request_schemas import EmailLoginRequest

router = APIRouter(prefix="/api/v1/auth", tags=["인증/인가 (회원가입 & 로그인)"])

# 재발급 요청에만 실리는 refresh 쿠키 (Path 가 /api/v1/auth/refresh). OpenAPI 에 기록하고,
# 없을 때의 응답은 공통 형식으로 직접 내므로 auto_error 는 끈다 (get_current_user 와 같은 방식).
# scheme_name 을 따로 주지 않으면 access 쿠키 방식과 같은 이름(APIKeyCookie)으로 기록되어 한쪽이 덮어써진다.
_refresh_token_cookie = APIKeyCookie(
    name=REFRESH_TOKEN_COOKIE, scheme_name="RefreshTokenCookie", auto_error=False
)


@router.post(
    "/login",
    status_code=status.HTTP_200_OK,
    summary="로그인",
    description=(
        "이메일·비밀번호를 확인하고 access_token / refresh_token 을 HttpOnly 쿠키로 발급합니다. "
        "응답 본문은 비어 있습니다. 패스워드는 Base64 인코딩된 상태로 전달되어야 합니다."
    ),
    responses={
        200: {
            "description": "로그인 성공. Set-Cookie 헤더로 access_token(Path=/), refresh_token(Path=/api/v1/auth/refresh) 발급",
        },
        400: {
            "model": ErrorResponse,
            "description": "비밀번호 형식 오류 (Base64 디코딩 실패)",
        },
        401: {
            "model": ErrorResponse,
            "description": "이메일 또는 비밀번호 불일치 (두 경우를 구분하지 않음)",
        },
        403: {"model": ErrorResponse, "description": "비활성화된 계정"},
        422: {
            "model": ErrorResponse,
            "description": "요청 형식 오류 (필수 필드 누락, 이메일 패턴 불일치 등)",
        },
    },
)
async def login(
    request: EmailLoginRequest,
    auth_service: AuthService = Depends(get_auth_service),
    settings: Settings = Depends(get_settings),
):
    """로그인 API 엔드포인트. 토큰은 본문이 아니라 쿠키로만 전달한다."""
    tokens = await auth_service.login(
        email=request.email,
        encoded_password=request.password,
    )

    response = Response(status_code=status.HTTP_200_OK)
    set_auth_cookies(
        response,
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        access_max_age=settings.jwt_config.access_token_expires,
        refresh_max_age=settings.jwt_config.refresh_token_expires,
        secure=settings.cookie_secure_enabled,
    )
    return response


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="로그아웃",
    description=(
        "access_token / refresh_token 쿠키를 발급 때와 같은 이름·Path 로 만료시킵니다(Max-Age=0). "
        "HttpOnly 쿠키는 프론트가 지울 수 없어 서버가 지웁니다. "
        "인증을 요구하지 않으며 쿠키가 없거나 만료되었어도 항상 204 입니다(멱등). "
        "서버는 토큰을 기록하지 않으므로 로그아웃 전에 복사된 토큰은 만료까지 유효합니다."
    ),
    responses={
        204: {
            "description": "로그아웃 완료. Set-Cookie 헤더로 access_token(Path=/), refresh_token(Path=/api/v1/auth/refresh) 만료",
        },
        403: {
            "model": ErrorResponse,
            "description": "허용되지 않은 Origin 의 요청 (INVALID_ORIGIN, 출처 검사 미들웨어)",
        },
    },
)
async def logout(settings: Settings = Depends(get_settings)):
    """로그아웃 API 엔드포인트. 서버 상태를 바꾸지 않고 두 쿠키의 삭제 지시만 내린다."""
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_auth_cookies(response, secure=settings.cookie_secure_enabled)
    return response


@router.post(
    "/refresh",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="접근 토큰 재발급 (API 27)",
    description=(
        "refresh_token 쿠키를 검증하고 DB 의 현재 역할·고객사로 access_token 쿠키를 새로 발급합니다. "
        "응답 본문은 비어 있고 Set-Cookie 로 access_token(Path=/)만 내립니다. "
        "refresh_token 쿠키는 그대로 두며(회전하지 않음) 로그인 뒤 7일이 지나면 다시 로그인해야 합니다. "
        "프론트는 보호된 API 가 401 UNAUTHENTICATED 를 돌려주면 이 API 를 호출한 뒤 원래 요청을 다시 보냅니다."
    ),
    responses={
        204: {
            "description": "재발급 완료. Set-Cookie 헤더로 access_token(Path=/) 발급",
        },
        401: {
            "model": ErrorResponse,
            "description": (
                "재발급 불가 (UNAUTHENTICATED): refresh_token 쿠키 없음·만료·위조, "
                "access 토큰을 보냄, 토큰의 사용자가 없음. 프론트는 로그인 화면으로 이동"
            ),
        },
        403: {
            "model": ErrorResponse,
            "description": (
                "비활성화된 계정 (ACCOUNT_DISABLED) 또는 허용되지 않은 Origin 의 요청 (INVALID_ORIGIN)"
            ),
        },
    },
)
async def refresh(
    refresh_token: str | None = Depends(_refresh_token_cookie),
    auth_service: AuthService = Depends(get_auth_service),
    settings: Settings = Depends(get_settings),
):
    """접근 토큰 재발급 API 엔드포인트. access 쿠키만 새로 내리고 refresh 쿠키는 건드리지 않는다."""
    if not refresh_token:
        raise UnauthenticatedException()

    access_token = await auth_service.refresh_access_token(refresh_token)

    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    set_access_cookie(
        response,
        access_token=access_token,
        max_age=settings.jwt_config.access_token_expires,
        secure=settings.cookie_secure_enabled,
    )
    return response
