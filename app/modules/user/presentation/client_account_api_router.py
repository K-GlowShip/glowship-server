from fastapi import APIRouter, Depends
from starlette import status

from app.core.exceptions import ErrorResponse
from app.core.security.auth import CurrentUser, require_roles
from app.modules.user.application.client_account_service import ClientAccountService
from app.modules.user.dependencies import get_client_account_service
from app.modules.user.infrastructure.user_enums import UserRoleCode
from app.modules.user.presentation.client_account_schemas import (
    ClientUserCreateRequest,
    ClientUserCreateResponse,
)

# 내부 관리자용 API. 고객사 범위를 토큰이 아니라 요청 본문(companyId)으로 받는 예외 경로다.
router = APIRouter(prefix="/api/v1/admin", tags=["고객사 계정 관리 (Admin)"])

# Ruff B008: 의존성 팩토리 호출은 기본 인자가 아니라 모듈 수준에서 한 번만 만든다.
require_admin = require_roles(UserRoleCode.ADMIN)


@router.post(
    "/users",
    status_code=status.HTTP_201_CREATED,
    summary="고객사 계정 생성",
    description=(
        "Glowship ADMIN 이 담당 고객사에 Client 계정(CLIENT_MEMBER·CLIENT_LEAD)을 생성합니다. "
        "요청자가 해당 고객사에 배정되어 있고 계정 관리 권한(can_manage_client_accounts)이 있어야 합니다. "
        "초기 비밀번호는 Base64 인코딩된 상태로 전달되어야 하며, 생성된 계정은 ACTIVE 상태입니다."
    ),
    response_model=ClientUserCreateResponse,
    responses={
        400: {
            "model": ErrorResponse,
            "description": "비밀번호 형식 오류 (Base64가 아니거나, 디코딩 결과가 영문·숫자 6~16자가 아님)",
        },
        401: {
            "model": ErrorResponse,
            "description": "로그인 필요 (access_token 쿠키 없음·만료)",
        },
        403: {
            "model": ErrorResponse,
            "description": "ADMIN 이 아니거나, 해당 고객사에 배정되지 않았거나, 계정 관리 권한이 없음. 허용되지 않은 Origin",
        },
        404: {"model": ErrorResponse, "description": "고객사 없음"},
        409: {"model": ErrorResponse, "description": "중복 이메일 또는 비활성 고객사"},
        422: {
            "model": ErrorResponse,
            "description": "요청 형식 오류 (필수 필드 누락, 이메일 패턴 불일치, Client 역할이 아닌 roleCode 등)",
        },
    },
)
async def create_client_user(
    request: ClientUserCreateRequest,
    current_user: CurrentUser = Depends(require_admin),
    service: ClientAccountService = Depends(get_client_account_service),
) -> ClientUserCreateResponse:
    """고객사 계정 생성 API 엔드포인트"""
    user = await service.create_client_user(
        actor=current_user,
        company_id=request.company_id,
        email=request.email,
        encoded_password=request.password,
        role_code=UserRoleCode(request.role_code),
        display_name=request.display_name,
    )
    return ClientUserCreateResponse.model_validate(user)
