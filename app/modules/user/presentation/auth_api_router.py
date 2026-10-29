from fastapi import APIRouter, Depends
from starlette import status
from starlette.responses import Response

from app.core.exceptions import ErrorResponse
from app.modules.user.application.auth_service import AuthService
from app.modules.user.dependencies import get_auth_service
from app.modules.user.presentation.auth_request_schemas import EmailLoginRequest

router = APIRouter(prefix="/api/v1", tags=["인증/인가 (회원가입 & 로그인)"])


@router.post(
    "/signup",
    status_code=status.HTTP_201_CREATED,  # 성공 시 201 Created 응답
    summary="Glowship 내부 운영자 회원가입",
    description="새로운 운영 관리자 계정을 생성합니다. 패스워드는 Base64 인코딩된 상태로 전달되어야 합니다.",
    responses={
        400: {
            "model": ErrorResponse,
            "description": "비밀번호 형식 오류 (Base64가 아니거나, 디코딩 결과가 영문·숫자 6~16자가 아님)",
        },
        409: {"model": ErrorResponse, "description": "중복 이메일"},
        422: {
            "model": ErrorResponse,
            "description": "요청 형식 오류 (필수 필드 누락, 이메일 패턴 불일치 등)",
        },
    },
)
async def signup_admin(
    request: EmailLoginRequest,
    user_service: AuthService = Depends(get_auth_service),
):
    """Glowship 내부 운영자 회원가입 API 엔드포인트"""
    await user_service.create_user(
        email=request.email,
        encoded_password=request.password,
    )

    return Response(status_code=status.HTTP_201_CREATED)


# 관리자 로그인
