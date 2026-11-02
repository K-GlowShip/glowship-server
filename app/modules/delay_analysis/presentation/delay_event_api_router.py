import uuid
from datetime import date

from fastapi import APIRouter, Depends, Query

from app.core.exceptions import ErrorResponse
from app.core.pagination import PageParams
from app.core.security.auth import CurrentUser, require_roles
from app.modules.delay_analysis.application.delay_event_query_service import (
    DelayEventQueryService,
    DelayEventTab,
)
from app.modules.delay_analysis.dependencies import get_delay_event_query_service
from app.modules.delay_analysis.presentation.delay_event_schemas import (
    DelayEventDetailResponse,
    DelayEventListResponse,
)
from app.modules.user.infrastructure.user_enums import UserRoleCode

# Client(고객사 팀원·팀장)용 조회 API. 고객사 범위는 로그인 토큰의 company_id 로 정한다.
router = APIRouter(prefix="/api/v1", tags=["지연 조회 (Client)"])

# Ruff B008: 의존성 팩토리 호출은 기본 인자가 아니라 모듈 수준에서 한 번만 만든다.
require_client = require_roles(UserRoleCode.CLIENT_MEMBER, UserRoleCode.CLIENT_LEAD)

_COMMON_ERRORS = {
    401: {
        "model": ErrorResponse,
        "description": "로그인 필요 (access_token 쿠키 없음·만료)",
    },
    403: {"model": ErrorResponse, "description": "Client 역할(팀원·팀장)이 아님"},
}


@router.get(
    "/delay-events",
    summary="지연 목록 (API 15)",
    description=(
        "고객사의 지연 건을 감지 시각 최신순(같으면 지연 ID 순)으로 반환합니다. 각 행에 현재 분석 실행(currentRun)과 최종 선택 여부를 붙이고, "
        "탭별 건수(counts)를 함께 줍니다. 감지 기간(from·to)은 UTC 날짜이며 to 날짜를 포함합니다. "
        "page·size 로 페이지를 나누고(기본 20, 최대 100), totalCount·totalPages 는 모든 필터를 적용한 값입니다."
    ),
    response_model=DelayEventListResponse,
    responses=_COMMON_ERRORS
    | {422: {"model": ErrorResponse, "description": "필터 형식 오류"}},
)
async def list_delay_events(
    status: DelayEventTab = Query(
        DelayEventTab.ALL,
        description=(
            "탭(현재 실행 상태 이름): ALL(전체), READY_FOR_REVIEW(선택 대기), ANALYZING(분석 중), "
            "ON_HOLD(분석 보류), NEEDS_REANALYSIS(재분석 필요: STALE·FAILED)"
        ),
    ),
    warehouse_id: uuid.UUID | None = Query(None, alias="warehouseId"),
    detected_from: date | None = Query(
        None, alias="from", description="감지일 시작 (UTC)"
    ),
    detected_to: date | None = Query(
        None, alias="to", description="감지일 끝 (UTC, 포함)"
    ),
    q: str | None = Query(
        None, max_length=50, description="선적 코드·SKU 코드·상품명 일부"
    ),
    has_final_decision: bool | None = Query(
        None,
        alias="hasFinalDecision",
        description="최종 선택 여부. 새 지연 알림은 false 로 조회",
    ),
    page: PageParams = Depends(),
    current_user: CurrentUser = Depends(require_client),
    service: DelayEventQueryService = Depends(get_delay_event_query_service),
) -> DelayEventListResponse:
    result = await service.list_delay_events(
        actor=current_user,
        tab=status,
        warehouse_id=warehouse_id,
        detected_from=detected_from,
        detected_to=detected_to,
        q=q,
        has_final_decision=has_final_decision,
        page=page,
    )
    return DelayEventListResponse.from_result(result)


@router.get(
    "/delay-events/{delay_event_id}",
    summary="지연 상세 (API 16)",
    description="지연 건의 선적·일정(계획·현재 예상, 감지 직전 예상)과 분석 실행 버전 이력을 반환합니다. 단계·입고일·지연 일수는 선적의 현재 값입니다.",
    response_model=DelayEventDetailResponse,
    responses=_COMMON_ERRORS
    | {404: {"model": ErrorResponse, "description": "지연 건 없음 (다른 고객사 포함)"}},
)
async def get_delay_event(
    delay_event_id: uuid.UUID,
    current_user: CurrentUser = Depends(require_client),
    service: DelayEventQueryService = Depends(get_delay_event_query_service),
) -> DelayEventDetailResponse:
    detail = await service.get_delay_event(
        actor=current_user, delay_event_id=delay_event_id
    )
    return DelayEventDetailResponse.of(detail)
