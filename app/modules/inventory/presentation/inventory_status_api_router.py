import uuid

from fastapi import APIRouter, Depends, Query

from app.core.exceptions import ErrorResponse
from app.core.pagination import PageParams
from app.core.security.tenant import TenantContext, get_tenant_context
from app.modules.inventory.application.inventory_status_service import (
    InventoryStatusService,
)
from app.modules.inventory.dependencies import get_inventory_status_service
from app.modules.inventory.presentation.inventory_status_schemas import (
    InventoryStatusResponse,
)

router = APIRouter(prefix="/api/v1/inventory-status", tags=["재고 (Client)"])


@router.get(
    "",
    summary="거점별 재고 현황 (API 14)",
    description=(
        "창고·SKU별 가용 수량, 하루 판매량, 안전재고 하회일, 품절 예상일, 상태(OK·CAUTION·RISK)를 돌려줍니다. "
        "최신 재고와 현재 운영 기준으로 조회할 때 계산하는 참고값이고, 입고 예정은 반영하지 않습니다(명세 D9·C22). "
        "기준일 D = 최신 재고 일자 + 1일, 하루 판매량 = 최근 salesBasisDays 일의 유효 주문 수요 평균(ORDER_DEMAND). "
        "정렬은 RISK → CAUTION → OK, 같으면 stockoutDate 빠른 순(null 은 뒤), 같으면 skuId·warehouseId 입니다. "
        "page·size 로 페이지를 나눕니다(기본 20, 최대 100). 고객사 범위는 access_token 쿠키의 company_id 입니다."
    ),
    response_model=InventoryStatusResponse,
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
            "description": "쿼리 파라미터 형식 오류 (UUID 형식, page < 1, size 범위 밖)",
        },
    },
)
async def get_inventory_status(
    warehouse_id: uuid.UUID | None = Query(
        default=None, alias="warehouseId", description="창고 ID"
    ),
    page: PageParams = Depends(),
    tenant: TenantContext = Depends(get_tenant_context),
    service: InventoryStatusService = Depends(get_inventory_status_service),
) -> InventoryStatusResponse:
    result = await service.get_page(tenant, warehouse_id=warehouse_id, page=page)
    return InventoryStatusResponse.from_result(result)
