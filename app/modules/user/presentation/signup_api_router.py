from fastapi import APIRouter, Depends
from starlette import status
from starlette.responses import Response

from app.core.exceptions import ErrorResponse
from app.modules.user.application.auth_service import AuthService
from app.modules.user.dependencies import get_auth_service
from app.modules.user.presentation.auth_request_schemas import EmailLoginRequest

# 개발용 라우터: prod 환경에서는 main.py 에서 등록하지 않습니다 (이슈 #78).
#   - 인증 없이 열려 있어 운영에 두면 누구나 운영자 계정을 만들 수 있다. 명세 D5: 공개 회원가입은 제공하지 않는다.
#   - users 에 직접 INSERT 하므로 앱 역할 연결(POSTGRES_ROLE)에서는 권한 오류로 실패한다.
#     역할을 고정하지 않은 로컬 연결(superuser)에서만 동작한다. 운영 계정은 DB 에 직접 넣는다(README).
# 경로는 로그인과 같은 /api/v1/auth 를 유지한다.
router = APIRouter(prefix="/api/v1/auth", tags=["인증/인가 (회원가입 & 로그인)"])


@router.post(
    "/signup",
    status_code=status.HTTP_201_CREATED,  # 성공 시 201 Created 응답
    summary="Glowship 내부 운영자 회원가입 (개발용)",
    description=(
        "새로운 운영 관리자 계정을 생성합니다. 패스워드는 Base64 인코딩된 상태로 전달되어야 합니다. "
        "개발용입니다: 운영(APP_ENV=prod) 서버에는 등록되지 않으며, "
        "POSTGRES_ROLE 을 비운 로컬 DB 연결에서만 동작합니다."
    ),
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
    """Glowship 내부 운영자 회원가입 API 엔드포인트 (개발용)"""
    await user_service.create_user(
        email=request.email,
        encoded_password=request.password,
    )

    return Response(status_code=status.HTTP_201_CREATED)
