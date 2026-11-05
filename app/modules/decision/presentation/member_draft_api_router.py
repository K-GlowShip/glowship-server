import uuid

from fastapi import APIRouter, Depends, Path
from starlette import status

from app.core.exceptions import ErrorResponse
from app.core.security.auth import require_roles
from app.core.security.tenant import TenantContext, get_tenant_context
from app.modules.decision.application.member_draft_service import MemberDraftService
from app.modules.decision.dependencies import get_member_draft_service
from app.modules.decision.presentation.member_draft_schemas import (
    MemberDraftCreateRequest,
    MemberDraftCreateResponse,
)
from app.modules.user.infrastructure.user_enums import UserRoleCode

# Client 팀원용 판단 기록 API. 고객사 범위는 로그인 토큰의 company_id 로 정한다.
router = APIRouter(prefix="/api/v1", tags=["판단 (Client)"])

# Ruff B008: 의존성 팩토리 호출은 기본 인자가 아니라 모듈 수준에서 한 번만 만든다.
# 의견·초안 저장은 팀원에게만 허용한다(명세 6절, D17). 팀장은 최종 선택(API 19)을 쓴다.
require_member = require_roles(UserRoleCode.CLIENT_MEMBER)


@router.post(
    "/analysis-runs/{runId}/drafts",
    status_code=status.HTTP_201_CREATED,
    summary="의견·선택 초안 저장 (API 18)",
    description=(
        "팀원이 분석 실행에 대해 고른 대응안(responseOptionId)이나 의견(opinionText)을 저장합니다. 둘 중 하나는 있어야 하고, "
        "의견은 앞뒤 공백을 뺀 뒤 1~600자입니다(C25). 판단 기록일 뿐 확정이 아니며 같은 실행에 여러 건 쓸 수 있습니다. "
        "지연 에피소드가 열려 있고 실행이 현재 실행(is_latest)이며 READY_FOR_REVIEW 일 때만 저장합니다(엔진 recheck.draft_check 순서). "
        "대응안은 같은 실행의 것이어야 하며 NEEDS_CHECK·NOT_FEASIBLE 도 고를 수 있습니다(C24). "
        "고객사 범위는 access_token 쿠키의 company_id 입니다."
    ),
    response_model=MemberDraftCreateResponse,
    responses={
        401: {
            "model": ErrorResponse,
            "description": "로그인 필요 (access_token 쿠키 없음·만료)",
        },
        403: {
            "model": ErrorResponse,
            "description": "팀원(CLIENT_MEMBER)이 아님. 허용되지 않은 Origin",
        },
        404: {
            "model": ErrorResponse,
            "description": "ANALYSIS_RUN_NOT_FOUND: 없는 실행·다른 고객사 실행",
        },
        409: {
            "model": ErrorResponse,
            "description": (
                "EPISODE_CLOSED(지연 종료), EXPLANATION_NOT_READY(설명 생성 중·설명 생성 실패), "
                "ANALYSIS_STALE, FINAL_DECISION_EXISTS, NOT_CURRENT_RUN, RUN_NOT_READY (명세 4.6 대응표, D22)"
            ),
        },
        422: {
            "model": ErrorResponse,
            "description": (
                "VALIDATION_ERROR(대응안·의견 둘 다 없음, 의견이 공백뿐이거나 600자 초과), "
                "OPTION_NOT_IN_RUN(다른 실행의 대응안)"
            ),
        },
    },
)
async def create_member_draft(
    request: MemberDraftCreateRequest,
    run_id: uuid.UUID = Path(alias="runId"),
    _member=Depends(require_member),
    tenant: TenantContext = Depends(get_tenant_context),
    service: MemberDraftService = Depends(get_member_draft_service),
) -> MemberDraftCreateResponse:
    saved = await service.save_draft(
        tenant,
        analysis_run_id=run_id,
        response_option_id=request.response_option_id,
        opinion_text=request.opinion_text,
    )
    return MemberDraftCreateResponse(
        member_draft_id=saved.member_draft_id, created_at=saved.created_at
    )
