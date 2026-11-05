import uuid

from fastapi import APIRouter, Depends

from app.core.exceptions import ErrorResponse
from app.core.security.auth import CurrentUser, require_roles
from app.modules.delay_analysis.application.analysis_run_detail_service import (
    AnalysisRunDetailService,
)
from app.modules.delay_analysis.dependencies import get_analysis_run_detail_service
from app.modules.delay_analysis.presentation.analysis_run_schemas import (
    AnalysisRunDetailResponse,
)
from app.modules.user.infrastructure.user_enums import UserRoleCode

# Client(고객사 팀원·팀장)용 조회 API. 고객사 범위는 로그인 토큰의 company_id 로 정한다.
router = APIRouter(prefix="/api/v1", tags=["분석 실행 조회 (Client)"])

# Ruff B008: 의존성 팩토리 호출은 기본 인자가 아니라 모듈 수준에서 한 번만 만든다.
require_client = require_roles(UserRoleCode.CLIENT_MEMBER, UserRoleCode.CLIENT_LEAD)


@router.get(
    "/analysis-runs/{run_id}",
    summary="분석 실행 상세 (API 17)",
    description=(
        "분석 실행 1건의 저장된 결과를 반환합니다(명세 v6.2 4.5). 실행·STALE·대상·입력 기준·재고 영향·위험 주문(주문별)·"
        "대응안 비교(outcomes·skuResults)·AI 설명(explanation.v1 저장 그대로)·실패 요약·판단 이력을 한 번에 줍니다. "
        "상태별로 결과가 없는 묶음은 빈 목록·null 입니다(NO_DELAY·ON_HOLD·도구 실패 FAILED 는 결과 행 없음). "
        "대상의 단계·입고일·지연 일수는 지연 목록과 같은 선적의 현재 값입니다. dataWarnings 는 아직 항상 빈 목록입니다."
    ),
    response_model=AnalysisRunDetailResponse,
    responses={
        401: {
            "model": ErrorResponse,
            "description": "로그인 필요 (access_token 쿠키 없음·만료)",
        },
        403: {"model": ErrorResponse, "description": "Client 역할(팀원·팀장)이 아님"},
        404: {
            "model": ErrorResponse,
            "description": "분석 실행 없음 (다른 고객사 실행 포함, ANALYSIS_RUN_NOT_FOUND)",
        },
    },
)
async def get_analysis_run(
    run_id: uuid.UUID,
    current_user: CurrentUser = Depends(require_client),
    service: AnalysisRunDetailService = Depends(get_analysis_run_detail_service),
) -> AnalysisRunDetailResponse:
    detail = await service.get_analysis_run(actor=current_user, run_id=run_id)
    return AnalysisRunDetailResponse.of(detail)
