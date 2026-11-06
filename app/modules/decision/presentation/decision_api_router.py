import uuid

from fastapi import APIRouter, Depends, Query

from app.core.exceptions import ErrorResponse
from app.core.pagination import PageParams
from app.core.query_types import IsoAwareDatetime
from app.core.security.tenant import TenantContext, get_tenant_context
from app.modules.decision.application.decision_list_service import (
    DecisionListService,
)
from app.modules.decision.dependencies import get_decision_list_service
from app.modules.decision.presentation.decision_schemas import (
    MAX_QUERY_LENGTH,
    DecisionListResponse,
    DecisionStatus,
    DecisionType,
)

router = APIRouter(prefix="/api/v1/decisions", tags=["판단 이력 (Client)"])


@router.get(
    "",
    summary="판단 이력 목록 (API 21)",
    description=(
        "의사결정 이력 화면의 목록입니다. 팀원 초안(member_drafts)과 팀장 최종 선택(final_decisions)을 한 목록으로 합쳐 "
        "판단 시각(decidedAt) 최신순(같으면 decisionId 순)으로 한 페이지씩 돌려줍니다. "
        "행마다 선적·대표 상품(지연 목록과 같은 D13 계산)·고른 대응안(의견만 쓴 초안이면 null)·판단자·분석 실행 요약이 붙습니다. "
        "status 는 FINAL(최종 선택), DRAFT(초안), NEEDS_REANALYSIS(초안인데 실행이 STALE)로 조회 때 계산합니다. "
        "기간 from·to 는 ISO 8601 UTC 시각이며 범위는 [from, to) 입니다(to 미포함, 명세 C18). "
        "시간대가 없는 값·날짜만·Unix 초 같은 숫자를 보내거나 from > to 이면 422 VALIDATION_ERROR 이고, from = to 는 0건입니다(C20). "
        "page·size 로 페이지를 나누고(기본 20, 최대 100), totalCount·totalPages 는 모든 필터를 적용한 값입니다. "
        "고객사 범위는 access_token 쿠키의 company_id 입니다. "
        "선택 이유·의견 본문과 당시 분석 수치는 GET /analysis-runs/{runId} 몫입니다."
    ),
    response_model=DecisionListResponse,
    responses={
        401: {
            "model": ErrorResponse,
            "description": "로그인 필요 (access_token 쿠키 없음·만료)",
        },
        403: {
            "model": ErrorResponse,
            "description": "고객사 범위 없음 (OWNER·ADMIN 토큰)",
        },
        422: {
            "model": ErrorResponse,
            "description": (
                "쿼리 파라미터 형식 오류 (시간대 없는·숫자 from·to, from > to, 허용되지 않은 decisionType·status, "
                "decidedBy·runId UUID 형식, page < 1, size 범위 밖, q 길이 초과)"
            ),
        },
    },
)
async def list_decisions(
    decided_from: IsoAwareDatetime | None = Query(
        default=None,
        alias="from",
        description="판단 시각 시작, 포함 (예: 2026-09-30T15:00:00Z = KST 10/01 0시)",
    ),
    decided_to: IsoAwareDatetime | None = Query(
        default=None,
        alias="to",
        description="판단 시각 끝, 미포함 (예: 2026-10-21T15:00:00Z = KST 10/21 까지 포함)",
    ),
    q: str | None = Query(
        default=None,
        max_length=MAX_QUERY_LENGTH,
        description="선적 코드, 또는 그 선적에 실린 SKU 코드·상품명 일부 (대소문자 무시)",
    ),
    decision_type: DecisionType | None = Query(
        default=None,
        alias="decisionType",
        description="DRAFT(초안)·FINAL(최종 선택). 화면 컨트롤은 없고 status 로 같은 구분이 된다",
    ),
    status: DecisionStatus | None = Query(
        default=None,
        description="DRAFT(초안)·FINAL(최종 선택)·NEEDS_REANALYSIS(재분석 필요)",
    ),
    decided_by: uuid.UUID | None = Query(
        default=None,
        alias="decidedBy",
        description="판단한 사용자 ID (작성자·선택자). 같은 고객사 사용자가 아니면 0건",
    ),
    run_id: uuid.UUID | None = Query(
        default=None,
        alias="runId",
        description="분석 실행 ID. 그 실행의 초안·최종 선택만",
    ),
    page: PageParams = Depends(),
    tenant: TenantContext = Depends(get_tenant_context),
    service: DecisionListService = Depends(get_decision_list_service),
) -> DecisionListResponse:
    """판단 이력 목록 조회 API 엔드포인트"""
    result = await service.get_page(
        tenant,
        decided_from=decided_from,
        decided_to=decided_to,
        decision_type=decision_type.value if decision_type else None,
        status=status.value if status else None,
        decided_by=decided_by,
        run_id=run_id,
        query=q,
        page=page,
    )
    return DecisionListResponse.from_result(result)
