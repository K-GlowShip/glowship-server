from fastapi import APIRouter, Depends

from app.core.exceptions import ErrorResponse
from app.core.security.tenant import TenantContext, get_tenant_context
from app.modules.data_sync.application.data_sync_status_service import (
    DataSyncStatusService,
)
from app.modules.data_sync.dependencies import get_data_sync_status_service
from app.modules.data_sync.presentation.data_sync_run_schemas import (
    DataSourceSyncStatusResponse,
    LatestDataSyncRunsResponse,
)

router = APIRouter(prefix="/api/v1/data-sync-runs", tags=["외부 데이터 수집"])


@router.get(
    "/latest",
    summary="원천 유형별 마지막 수집 상태 조회",
    description=(
        "원천 유형(ORDER·INVENTORY·SHIPMENT·PRODUCT·WAREHOUSE)별로 가장 최근 수집 실행 1건과 "
        "미해결 검증 이슈 수, 운영 설정 값(원천 시스템 이름·수집 주기), 서버가 판정한 동기화 여부를 돌려줍니다. "
        "고객사 범위는 access_token 쿠키의 company_id 입니다. "
        "수집 이력이 없는 원천도 실행 필드 null, isSynced=false 로 항상 다섯 항목이 내려갑니다."
    ),
    response_model=LatestDataSyncRunsResponse,
    responses={
        401: {
            "model": ErrorResponse,
            "description": "로그인 필요 (access_token 쿠키 없음·만료)",
        },
        403: {
            "model": ErrorResponse,
            "description": "고객사 범위 없음 (OWNER·ADMIN 토큰)",
        },
    },
)
async def get_latest_data_sync_runs(
    tenant: TenantContext = Depends(get_tenant_context),
    service: DataSyncStatusService = Depends(get_data_sync_status_service),
) -> LatestDataSyncRunsResponse:
    """원천 유형별 마지막 수집 상태 조회 API 엔드포인트"""
    statuses = await service.get_latest_statuses(tenant)
    return LatestDataSyncRunsResponse(
        items=[
            DataSourceSyncStatusResponse.model_validate(status) for status in statuses
        ]
    )
