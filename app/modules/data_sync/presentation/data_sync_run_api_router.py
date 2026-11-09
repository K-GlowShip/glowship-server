from fastapi import APIRouter, BackgroundTasks, Depends
from starlette import status

from app.core.exceptions import ErrorResponse
from app.core.security.auth import require_roles
from app.core.security.tenant import TenantContext, get_tenant_context
from app.modules.data_sync.application.data_sync_status_service import (
    DataSyncStatusService,
)
from app.modules.data_sync.application.exception.data_sync_exceptions import (
    NextDayNeedsVirtualClockException,
)
from app.modules.data_sync.application.manual_sync_service import ManualSyncService
from app.modules.data_sync.dependencies import (
    get_data_sync_status_service,
    get_manual_sync_service,
)
from app.modules.data_sync.infrastructure.data_sync_enums import (
    DataSyncRunStatus,
    SyncAdvance,
)
from app.modules.data_sync.presentation.data_sync_run_schemas import (
    DataSourceSyncStatusResponse,
    DataSyncRunCreateRequest,
    DataSyncRunStartedResponse,
    LatestDataSyncRunsResponse,
)
from app.modules.user.infrastructure.user_enums import UserRoleCode

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


# Ruff B008: 의존성 팩토리 호출은 기본 인자가 아니라 모듈 수준에서 한 번만 만든다.
# 수동 동기화는 Client 팀원·팀장 모두 실행할 수 있다 (명세 6절).
require_client = require_roles(UserRoleCode.CLIENT_MEMBER, UserRoleCode.CLIENT_LEAD)


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    summary="수동 동기화 실행 (API 25)",
    description=(
        "어느 카드(sourceSystem: ERP·WMS·TMS)를 눌러도 다섯 원천을 상품 → 창고 → 주문 → 선적 → 재고 순서로 함께 수집합니다"
        "(원천마다 검증 → 변경 반영 → 적용 순번 → 지연 감지 → STALE 판정, 자동 수집과 같은 절차). "
        "요청 안에서 다섯 수집을 RUNNING 으로 기록하고 202 와 원천별 실행 ID 배열을 돌려준 뒤, 원천 응답 받기와 적용은 백그라운드에서 합니다. "
        "결과는 GET /data-sync-runs/latest 를 다시 조회해 봅니다. "
        "가상 시각(데모)이면 한 번에 고객사 시계가 10분 나아가고(advance=TICK), advance=NEXT_DAY 는 다음 자정 tick 까지 이어서 수집합니다. "
        "가상 시각이 아니면 서버의 지금 시각으로 수집하고 NEXT_DAY 는 422 입니다. "
        "sourceDomain 은 이전 요청 모양(하위 호환)으로 sourceSystem 과 같은 동작이며 둘 중 하나만 보냅니다. "
        "고객사 범위는 access_token 쿠키의 company_id 입니다."
    ),
    response_model=list[DataSyncRunStartedResponse],
    responses={
        401: {
            "model": ErrorResponse,
            "description": "로그인 필요 (access_token 쿠키 없음·만료)",
        },
        403: {
            "model": ErrorResponse,
            "description": "Client 역할(팀원·팀장)이 아님. 허용되지 않은 Origin",
        },
        409: {
            "model": ErrorResponse,
            "description": "SYNC_IN_PROGRESS: 그 고객사에 실행 중인 수집이 있음(다섯 원천 중 하나라도)",
        },
        422: {
            "model": ErrorResponse,
            "description": (
                "VALIDATION_ERROR: 요청 형식 오류(sourceSystem·sourceDomain 이 둘 다 있거나 둘 다 없음 포함), "
                "또는 가상 시각이 아닌데 advance=NEXT_DAY"
            ),
        },
    },
)
async def create_data_sync_run(
    request: DataSyncRunCreateRequest,
    background_tasks: BackgroundTasks,
    _client=Depends(require_client),
    tenant: TenantContext = Depends(get_tenant_context),
    service: ManualSyncService = Depends(get_manual_sync_service),
) -> list[DataSyncRunStartedResponse]:
    if request.advance == SyncAdvance.NEXT_DAY and service.virtual_start is None:
        raise NextDayNeedsVirtualClockException()
    ticket = await service.request_tick(tenant)
    if request.advance == SyncAdvance.NEXT_DAY:
        background_tasks.add_task(service.run_until_midnight, tenant, ticket)
    else:
        background_tasks.add_task(service.run_tick, ticket)
    return [
        DataSyncRunStartedResponse(
            source_domain=run.source_domain,
            data_sync_run_id=run.data_sync_run_id,
            status=DataSyncRunStatus.RUNNING,
        )
        for run in ticket.started
    ]
