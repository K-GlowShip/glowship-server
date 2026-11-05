from fastapi import APIRouter, Depends

from app.core.exceptions import ErrorResponse
from app.core.security.auth import CurrentUser, get_current_user
from app.modules.user.application.me_service import MeService
from app.modules.user.dependencies import get_me_service
from app.modules.user.presentation.me_schemas import MeResponse

# 모든 역할이 호출하는 공통 헤더용 API. 고객사 범위 의존성(get_tenant_context)은 OWNER·ADMIN 을 403 으로 막으므로
# 쿠키 검증만 하는 get_current_user 를 쓴다.
router = APIRouter(prefix="/api/v1", tags=["내 계정"])


@router.get(
    "/me",
    summary="내 계정 조회 (API 3)",
    description=(
        "access_token 쿠키의 사용자를 DB 에서 다시 읽어 계정·역할·소속 고객사명을 돌려줍니다. "
        "로그인 응답에는 사용자 정보가 없으므로 로그인 직후 화면이 이 API 를 호출합니다. "
        "OWNER·ADMIN 은 소속 고객사가 없어 companyId·companyName 이 null 입니다. "
        "명세 4.1 의 email 과 ADMIN 의 assignedCompanies 는 프론트 합의로 응답에 없습니다."
    ),
    response_model=MeResponse,
    responses={
        401: {
            "model": ErrorResponse,
            "description": "로그인 필요 (access_token 쿠키 없음·만료, 토큰의 사용자가 없음)",
        },
        403: {
            "model": ErrorResponse,
            "description": "비활성화된 계정 (ACCOUNT_DISABLED)",
        },
    },
)
async def get_me(
    current_user: CurrentUser = Depends(get_current_user),
    service: MeService = Depends(get_me_service),
) -> MeResponse:
    """내 계정 조회 API 엔드포인트"""
    profile = await service.get_me(current_user)
    return MeResponse.of(profile)
