import uuid

from fastapi import APIRouter, Depends, Header, Path
from starlette import status

from app.core.exceptions import ErrorResponse
from app.core.security.auth import require_roles
from app.core.security.tenant import TenantContext, get_tenant_context
from app.modules.decision.application.exception.decision_exceptions import (
    DecisionErrorCode,
    DecisionException,
)
from app.modules.decision.application.final_decision_service import (
    FinalDecisionService,
)
from app.modules.decision.dependencies import get_final_decision_service
from app.modules.decision.presentation.final_decision_schemas import (
    FinalDecisionCreateRequest,
    FinalDecisionCreateResponse,
)
from app.modules.user.infrastructure.user_enums import UserRoleCode

# Client 팀장용 최종 선택 API. 고객사 범위는 로그인 토큰의 company_id 로 정한다.
router = APIRouter(prefix="/api/v1", tags=["판단 (Client)"])

# Ruff B008: 의존성 팩토리 호출은 기본 인자가 아니라 모듈 수준에서 한 번만 만든다.
# 최종 선택은 팀장에게만 허용한다(명세 6절, D17). 팀원은 의견·초안(API 18)을 쓴다.
require_lead = require_roles(UserRoleCode.CLIENT_LEAD)


@router.post(
    "/analysis-runs/{runId}/final-decision",
    status_code=status.HTTP_201_CREATED,
    summary="최종 대응안 선택 (API 19)",
    description=(
        "팀장이 분석 실행의 대응안 하나를 최종 선택합니다. Idempotency-Key 헤더가 필수이고, 같은 키로 다시 보내면 저장된 결과를 그대로 돌려줍니다. "
        "실행 잠금 안에서 재확인합니다(지연 열림, 설명 생성 실패 아님, 현재 실행이고 선택 대기, 운영 기준 버전, FEASIBLE 대응안, 선택 시점 시간 확인). "
        "시간이 지나 대응안이 맞지 않으면 실행을 STALE(TIME_ELAPSED)로 바꾼 뒤 409 ANALYSIS_STALE 로 거부하므로 재분석이 필요합니다. "
        "reasonText 는 앞뒤 공백을 뺀 뒤 1~600자입니다(C25). 실제 출고·운송 요청은 하지 않습니다(BR-014). "
        "고객사 범위는 access_token 쿠키의 company_id 입니다."
    ),
    response_model=FinalDecisionCreateResponse,
    responses={
        400: {
            "model": ErrorResponse,
            "description": "IDEMPOTENCY_KEY_REQUIRED: Idempotency-Key 헤더 없음",
        },
        401: {
            "model": ErrorResponse,
            "description": "로그인 필요 (access_token 쿠키 없음·만료)",
        },
        403: {
            "model": ErrorResponse,
            "description": "팀장(CLIENT_LEAD)이 아님. 허용되지 않은 Origin",
        },
        404: {
            "model": ErrorResponse,
            "description": "ANALYSIS_RUN_NOT_FOUND: 없는 실행·다른 고객사 실행",
        },
        409: {
            "model": ErrorResponse,
            "description": (
                "EPISODE_CLOSED, EXPLANATION_NOT_READY, ANALYSIS_STALE(STALE 이거나 시간 경과 TIME_ELAPSED), "
                "FINAL_DECISION_EXISTS, NOT_CURRENT_RUN, RUN_NOT_READY, OPERATING_RULE_CHANGED, "
                "IDEMPOTENCY_KEY_REUSED(같은 키를 다른 실행이나 다른 본문에 씀)"
            ),
        },
        422: {
            "model": ErrorResponse,
            "description": (
                "VALIDATION_ERROR(reasonText 없음·공백뿐·600자 초과, 형식 오류), "
                "OPTION_NOT_IN_RUN(다른 실행의 대응안), OPTION_NOT_FEASIBLE(FEASIBLE 이 아님)"
            ),
        },
    },
)
async def create_final_decision(
    request: FinalDecisionCreateRequest,
    run_id: uuid.UUID = Path(alias="runId"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    _lead=Depends(require_lead),
    tenant: TenantContext = Depends(get_tenant_context),
    service: FinalDecisionService = Depends(get_final_decision_service),
) -> FinalDecisionCreateResponse:
    if idempotency_key is None or not idempotency_key.strip():
        raise DecisionException(DecisionErrorCode.IDEMPOTENCY_KEY_REQUIRED)
    result = await service.decide(
        tenant,
        analysis_run_id=run_id,
        response_option_id=request.response_option_id,
        reason_text=request.reason_text,
        idempotency_key=idempotency_key.strip(),
    )
    return FinalDecisionCreateResponse(
        final_decision_id=result.final_decision_id,
        decided_at=result.decided_at,
        checked_seq=result.checked_seq,
        status=result.status,
    )
