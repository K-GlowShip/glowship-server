import uuid

from fastapi import APIRouter, Body, Depends, Path
from starlette import status

from app.core.exceptions import ErrorResponse
from app.core.security.auth import require_roles
from app.core.security.tenant import TenantContext, get_tenant_context
from app.modules.decision.application.reanalysis_service import ReanalysisService
from app.modules.decision.dependencies import get_reanalysis_service
from app.modules.decision.presentation.reanalysis_schemas import (
    ReanalysisCreateRequest,
    ReanalysisCreateResponse,
)
from app.modules.delay_analysis.application.run_start.app_runner import (
    get_analysis_runner,
)
from app.modules.delay_analysis.application.run_start.runner import AnalysisRunner
from app.modules.user.infrastructure.user_enums import UserRoleCode

# Client 팀원·팀장용 재분석 요청 API. 고객사 범위는 로그인 토큰의 company_id 로 정한다.
router = APIRouter(prefix="/api/v1", tags=["판단 (Client)"])

# Ruff B008: 의존성 팩토리 호출은 기본 인자가 아니라 모듈 수준에서 한 번만 만든다.
# 재분석 요청은 팀원·팀장 모두 할 수 있다(명세 6절).
require_client = require_roles(UserRoleCode.CLIENT_MEMBER, UserRoleCode.CLIENT_LEAD)


@router.post(
    "/analysis-runs/{runId}/reanalysis",
    status_code=status.HTTP_202_ACCEPTED,
    summary="재분석 요청 (API 20)",
    description=(
        "현재 분석 실행을 다시 분석하도록 새 실행(버전 + 1)을 만듭니다. 새 실행은 DETECTED 이고 분석은 입력 확정부터 다시 시작합니다. "
        "허용 상태는 READY_FOR_REVIEW·STALE·NO_RISK·ON_HOLD·FAILED 입니다(D10). 이전 실행은 현재 실행 표시만 내리고 상태·결과·판단은 그대로 둡니다(BR-012). "
        "본문은 선택이며 requestReason 을 보낼 수 있습니다. 고객사 범위는 access_token 쿠키의 company_id 입니다."
    ),
    response_model=ReanalysisCreateResponse,
    responses={
        401: {
            "model": ErrorResponse,
            "description": "로그인 필요 (access_token 쿠키 없음·만료)",
        },
        403: {
            "model": ErrorResponse,
            "description": "Client 역할(팀원·팀장)이 아님. 허용되지 않은 Origin",
        },
        404: {
            "model": ErrorResponse,
            "description": "ANALYSIS_RUN_NOT_FOUND: 없는 실행·다른 고객사 실행",
        },
        409: {
            "model": ErrorResponse,
            "description": (
                "EPISODE_CLOSED(지연 종료), NOT_CURRENT_RUN(현재 실행 아님), RUN_IN_PROGRESS(분석 중), "
                "RUN_FINALIZED(최종 선택 끝남), REANALYSIS_NOT_ALLOWED(NO_DELAY)"
            ),
        },
        422: {"model": ErrorResponse, "description": "VALIDATION_ERROR: 형식 오류"},
    },
)
async def request_reanalysis(
    request: ReanalysisCreateRequest | None = Body(default=None),
    run_id: uuid.UUID = Path(alias="runId"),
    _client=Depends(require_client),
    tenant: TenantContext = Depends(get_tenant_context),
    service: ReanalysisService = Depends(get_reanalysis_service),
    runner: AnalysisRunner = Depends(get_analysis_runner),
) -> ReanalysisCreateResponse:
    result = await service.request(
        tenant,
        analysis_run_id=run_id,
        request_reason=request.request_reason if request else None,
    )
    # 서비스가 새 실행을 커밋한 뒤 분석을 시작한다(자동 시작이 꺼져 있으면 DETECTED 그대로)
    runner.start(tenant.company_id, result.run_id)
    return ReanalysisCreateResponse(
        run_id=result.run_id,
        version_no=result.version_no,
        parent_run_id=result.parent_run_id,
        status=result.status,
    )
