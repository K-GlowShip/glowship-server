import uuid

from fastapi import APIRouter, Depends, Query
from starlette import status

from app.core.exceptions import ErrorResponse
from app.core.security.auth import CurrentUser, require_roles
from app.modules.operating_rule.application.operating_rule_service import (
    OperatingRuleService,
)
from app.modules.operating_rule.dependencies import get_operating_rule_service
from app.modules.operating_rule.presentation.operating_rule_schemas import (
    OperatingRuleSetCreateRequest,
    OperatingRuleSetCreateResponse,
    OperatingRuleSetListResponse,
    OperatingRuleSetResponse,
)
from app.modules.user.infrastructure.user_enums import UserRoleCode

# 내부 관리자용 API. 고객사 범위를 토큰이 아니라 요청(companyId)으로 받는다(계정 생성 API 와 같은 예외 경로).
router = APIRouter(
    prefix="/api/v1/admin/operating-rule-sets", tags=["운영 기준 (Admin)"]
)

# Ruff B008: 의존성 팩토리 호출은 기본 인자가 아니라 모듈 수준에서 한 번만 만든다.
require_admin = require_roles(UserRoleCode.ADMIN)

_AUTH_RESPONSES = {
    401: {
        "model": ErrorResponse,
        "description": "로그인 필요 (access_token 쿠키 없음·만료)",
    },
    403: {
        "model": ErrorResponse,
        "description": "ADMIN 이 아니거나 해당 고객사에 배정되지 않음. 허용되지 않은 Origin",
    },
    404: {
        "model": ErrorResponse,
        "description": "COMPANY_NOT_FOUND: 고객사 없음",
    },
    409: {
        "model": ErrorResponse,
        "description": "COMPANY_INACTIVE: 비활성 고객사",
    },
}


@router.get(
    "",
    summary="운영 기준 버전 목록 (API 4)",
    description=(
        "Glowship ADMIN 이 담당 고객사의 운영 기준 버전을 버전 내림차순으로 조회합니다. "
        "요청자가 해당 고객사에 배정되어 있어야 합니다. 버전마다 리드타임(leadTimes)을 함께 돌려주고, "
        "isCurrent 는 지금 적용 중인 버전(적용 시작일이 지난 것 중 버전이 가장 큰 것) 하나만 true 입니다."
    ),
    response_model=OperatingRuleSetListResponse,
    responses={
        **_AUTH_RESPONSES,
        422: {
            "model": ErrorResponse,
            "description": "요청 형식 오류 (companyId 없음·UUID 아님)",
        },
    },
)
async def list_operating_rule_sets(
    company_id: uuid.UUID = Query(
        alias="companyId", description="조회할 고객사. 요청자가 배정된 고객사여야 한다."
    ),
    current_user: CurrentUser = Depends(require_admin),
    service: OperatingRuleService = Depends(get_operating_rule_service),
) -> OperatingRuleSetListResponse:
    """운영 기준 버전 목록 API 엔드포인트"""
    details = await service.list_rule_sets(actor=current_user, company_id=company_id)
    return OperatingRuleSetListResponse(
        items=[OperatingRuleSetResponse.of(detail) for detail in details]
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="운영 기준 새 버전 저장 (API 5)",
    description=(
        "Glowship ADMIN 이 담당 고객사의 운영 기준을 새 버전으로 저장합니다. 기존 버전은 수정·삭제하지 않고 "
        "버전 번호를 1 올려 새로 만듭니다. 요청자가 해당 고객사에 배정되어 있어야 합니다. "
        "저장만으로는 기존 분석 실행을 STALE 로 바꾸지 않습니다. 적용 시작일이 지난 뒤 팀장이 이전 버전으로 분석한 실행을 "
        "최종 선택하면 409 OPERATING_RULE_CHANGED 로 거부되어 재분석이 필요합니다. "
        "분석 계산은 판매 기준 28일·CALENDAR_DAY 리드타임만 지원하며, 다른 값이 계산에 쓰이면 그 분석은 보류(ON_HOLD)됩니다."
    ),
    response_model=OperatingRuleSetCreateResponse,
    responses={
        **_AUTH_RESPONSES,
        400: {
            "model": ErrorResponse,
            "description": (
                "INVALID_LEAD_TIME: 유형별 toRegionCode 규칙 위반(CUSTOMER_DELIVERY·AIR_TRANSIT 만 필수), "
                "같은 키(유형·창고·도착 권역) 중복, 이 고객사의 창고가 아님"
            ),
        },
        422: {
            "model": ErrorResponse,
            "description": "요청 형식 오류 (필수 필드 누락, 0 이하 일수, 음수 리드타임, 허용되지 않은 유형·기준·권역 등)",
        },
    },
)
async def create_operating_rule_set(
    request: OperatingRuleSetCreateRequest,
    current_user: CurrentUser = Depends(require_admin),
    service: OperatingRuleService = Depends(get_operating_rule_service),
) -> OperatingRuleSetCreateResponse:
    """운영 기준 새 버전 저장 API 엔드포인트"""
    created = await service.create_rule_set(
        actor=current_user,
        company_id=request.company_id,
        effective_from=request.effective_date,
        safety_stock_days=request.safety_stock_days,
        sales_basis_days=request.sales_basis_days,
        allow_partial_fulfillment=request.is_partial_fulfillment_allowed,
        lead_times=[lead_time.to_new() for lead_time in request.lead_times],
    )
    return OperatingRuleSetCreateResponse.of(created)
