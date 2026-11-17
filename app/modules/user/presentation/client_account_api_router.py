import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query, Response
from starlette import status

from app.core.exceptions import ErrorResponse
from app.core.pagination import Page, PageParams
from app.core.security.auth import CurrentUser, require_roles
from app.modules.user.application.client_account_service import ClientAccountService
from app.modules.user.dependencies import get_client_account_service
from app.modules.user.infrastructure.user_enums import UserRoleCode, UserStatus
from app.modules.user.presentation.client_account_schemas import (
    ClientUserCreateRequest,
    ClientUserCreateResponse,
    ClientUserListItemResponse,
    ClientUserPasswordResetRequest,
)

# 내부 관리자용 API. 고객사 범위를 토큰이 아니라 요청(생성은 본문, 목록은 쿼리의 companyId)으로 받는 예외 경로다.
# 비밀번호 재설정은 계정(userId)만 받고, 그 계정의 고객사에 요청자가 배정되어 있는지는 DB 함수가 확인한다.
router = APIRouter(prefix="/api/v1/admin", tags=["고객사 계정 관리 (Admin)"])

# Ruff B008: 의존성 팩토리 호출은 기본 인자가 아니라 모듈 수준에서 한 번만 만든다.
require_admin = require_roles(UserRoleCode.ADMIN)


@router.get(
    "/users",
    summary="고객사 계정 목록 (API 28)",
    description=(
        "Glowship ADMIN 이 담당 고객사의 Client 계정(CLIENT_MEMBER·CLIENT_LEAD)을 최근에 만든 순서로 조회합니다. "
        "ADMIN·OWNER 계정은 나오지 않습니다. 요청자가 해당 고객사에 배정되어 있으면 되고, "
        "계정 관리 권한(can_manage_client_accounts)이 없어도 조회할 수 있습니다."
    ),
    response_model=Page[ClientUserListItemResponse],
    responses={
        401: {
            "model": ErrorResponse,
            "description": "로그인 필요 (access_token 쿠키 없음·만료)",
        },
        403: {
            "model": ErrorResponse,
            "description": "ADMIN 이 아니거나, 해당 고객사에 배정되지 않음 (없는 고객사도 같은 응답)",
        },
        404: {"model": ErrorResponse, "description": "고객사 없음"},
        409: {"model": ErrorResponse, "description": "비활성 고객사"},
        422: {
            "model": ErrorResponse,
            "description": "요청 형식 오류 (companyId 없음·UUID 아님, Client 역할이 아닌 roleCode, 없는 status, page·size 범위 밖)",
        },
    },
)
async def list_client_users(
    company_id: uuid.UUID = Query(
        alias="companyId", description="조회할 고객사. 요청자가 배정된 고객사여야 한다."
    ),
    # Client 역할만 허용한다. OWNER·ADMIN 을 넣으면 요청 형식 오류(422)로 거절된다.
    role_code: Literal["CLIENT_MEMBER", "CLIENT_LEAD"] | None = Query(
        default=None, alias="roleCode", description="역할로 거르기. 생략하면 둘 다"
    ),
    account_status: UserStatus | None = Query(
        default=None, alias="status", description="계정 상태로 거르기. 생략하면 둘 다"
    ),
    params: PageParams = Depends(),
    current_user: CurrentUser = Depends(require_admin),
    service: ClientAccountService = Depends(get_client_account_service),
) -> Page[ClientUserListItemResponse]:
    """고객사 계정 목록 API 엔드포인트"""
    users, total_count = await service.list_client_users(
        actor=current_user,
        company_id=company_id,
        role_code=UserRoleCode(role_code) if role_code is not None else None,
        status=account_status,
        params=params,
    )
    return Page[ClientUserListItemResponse].of(
        [ClientUserListItemResponse.model_validate(user) for user in users],
        total_count,
        params,
    )


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


@router.post(
    "/users/{user_id}/password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="고객사 계정 비밀번호 재설정 (API 29)",
    description=(
        "Glowship ADMIN 이 담당 고객사 Client 계정(CLIENT_MEMBER·CLIENT_LEAD)의 비밀번호를 새 값으로 바꿉니다. "
        "요청자가 그 계정의 고객사에 배정되어 있고 계정 관리 권한(can_manage_client_accounts)이 있어야 합니다. "
        "새 비밀번호는 Base64 인코딩된 상태로 전달되어야 합니다. 이미 발급된 토큰은 폐기하지 않아, "
        "재설정 뒤에도 기존 access 토큰은 최대 30분·refresh 토큰은 최대 7일 유효합니다."
    ),
    responses={
        204: {"description": "재설정 완료. 본문 없음"},
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
            "description": "ADMIN 이 아니거나, 그 계정의 고객사에 배정은 있으나 계정 관리 권한이 없음. 허용되지 않은 Origin",
        },
        404: {
            "model": ErrorResponse,
            "description": "USER_NOT_FOUND: 없는 계정, 배정되지 않은 고객사의 계정, Client 역할이 아닌 계정 (구분 없음)",
        },
        422: {
            "model": ErrorResponse,
            "description": "요청 형식 오류 (userId 가 UUID 아님, password 누락)",
        },
    },
)
async def reset_client_user_password(
    user_id: uuid.UUID,
    request: ClientUserPasswordResetRequest,
    current_user: CurrentUser = Depends(require_admin),
    service: ClientAccountService = Depends(get_client_account_service),
) -> Response:
    """고객사 계정 비밀번호 재설정 API 엔드포인트"""
    await service.reset_client_user_password(
        actor=current_user, user_id=user_id, encoded_password=request.password
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
