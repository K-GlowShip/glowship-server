import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.exceptions import ErrorResponse
from app.core.security.tenant import TenantContext, get_tenant_context
from app.modules.dashboard.application.dashboard_query_service import (
    DashboardQueryService,
    InboundTrendDays,
)
from app.modules.dashboard.dependencies import get_dashboard_query_service
from app.modules.dashboard.presentation.dashboard_schemas import (
    DashboardSummaryResponse,
    DaysQuery,
    InboundTrendResponse,
)

router = APIRouter(prefix="/api/v1/dashboard", tags=["대시보드 (Client)"])

_COMMON_ERRORS = {
    401: {
        "model": ErrorResponse,
        "description": "로그인 필요 (access_token 쿠키 없음·만료)",
    },
    403: {
        "model": ErrorResponse,
        "description": "고객사 범위 없음 (OWNER·ADMIN 토큰)",
    },
}


@router.get(
    "/summary",
    summary="대시보드 요약 (API 22)",
    description=(
        "KPI 4개(진행 중인 선적, 지연 선적, 정시 입고율, 위험 주문)와 각 비교값, 기준 시각 lastSyncedAt 을 돌려줍니다. "
        "기준 시각 T 는 고객사의 마지막 적재 수집 완료 시각이고 서버의 지금 시각이 아닙니다(명세 C22). "
        "비교값: 진행 중인 선적은 T − 7일 대비(weekOverWeek), 지연 선적은 T − 1일 대비(dayOverDay), "
        "정시 입고율은 직전 30일 대비(monthOverMonth). 비율은 0~1 입니다. "
        "적재된 수집이 없으면 lastSyncedAt 은 null, 건수는 0, 비율·비교값은 null 입니다. "
        "조회할 때 계산하고 저장하지 않습니다. 고객사 범위는 access_token 쿠키의 company_id 입니다."
    ),
    response_model=DashboardSummaryResponse,
    responses=_COMMON_ERRORS,
)
async def get_dashboard_summary(
    tenant: TenantContext = Depends(get_tenant_context),
    service: DashboardQueryService = Depends(get_dashboard_query_service),
) -> DashboardSummaryResponse:
    return DashboardSummaryResponse.of(await service.get_summary(tenant))


@router.get(
    "/inbound-trend",
    summary="입고 추이 (API 23)",
    description=(
        "기간별 입고 예정·실제 입고 선적 수를 날짜마다 돌려줍니다. 기간은 [기준일 − days + 1일, 기준일]이고 "
        "0건인 날도 0으로 채웁니다. 기준일은 고객사의 마지막 수집 완료 시각의 UTC 날짜입니다(서버의 지금 날짜가 아님, 명세 C22). "
        "적재된 수집이 없으면 points 는 []입니다. 고객사 범위는 access_token 쿠키의 company_id 입니다."
    ),
    response_model=InboundTrendResponse,
    responses=_COMMON_ERRORS
    | {
        422: {
            "model": ErrorResponse,
            "description": "쿼리 파라미터 형식 오류 (days 가 7·30·90 이 아님, UUID 형식)",
        }
    },
)
async def get_inbound_trend(
    # Annotated 안에 Query 를 둬야 DaysQuery 의 BeforeValidator 가 적용된다 (= Query(...) 기본값 형식이면 "07"·"7.0" 이 통과함)
    days: Annotated[
        DaysQuery, Query(description="기간(일): 7·30·90, 기본 7")
    ] = InboundTrendDays.WEEK,
    warehouse_id: uuid.UUID | None = Query(
        default=None, alias="warehouseId", description="입고 창고 ID"
    ),
    tenant: TenantContext = Depends(get_tenant_context),
    service: DashboardQueryService = Depends(get_dashboard_query_service),
) -> InboundTrendResponse:
    result = await service.get_inbound_trend(
        tenant, days=days, warehouse_id=warehouse_id
    )
    return InboundTrendResponse.from_result(result)
