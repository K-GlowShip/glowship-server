from fastapi import APIRouter, Depends
from starlette import status
from starlette.responses import Response

from app.core.config import Settings, get_settings
from app.core.exceptions import ErrorResponse
from app.core.security.cookies import clear_auth_cookies, set_auth_cookies
from app.modules.user.application.auth_service import AuthService
from app.modules.user.dependencies import get_auth_service
from app.modules.user.presentation.auth_request_schemas import EmailLoginRequest

router = APIRouter(prefix="/api/v1/auth", tags=["인증/인가 (회원가입 & 로그인)"])


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
