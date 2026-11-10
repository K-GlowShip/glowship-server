import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.exceptions import ErrorResponse
from app.core.pagination import PageParams
from app.core.schemas import IsoDate
from app.core.security.tenant import TenantContext, get_tenant_context
from app.core.text_input import SearchText
from app.modules.inventory.application.inventory_list_service import (
    InventoryListService,
)
from app.modules.inventory.dependencies import get_inventory_list_service
from app.modules.inventory.presentation.inventory_schemas import (
    MAX_QUERY_LENGTH,
    InventoryListResponse,
)

router = APIRouter(prefix="/api/v1/inventory", tags=["재고 (Client)"])


@router.get(
    "",
    summary="재고 목록 (API 11)",
    description=(
        "고객사의 한 일자(date) 재고를 창고 코드 → SKU 코드 순(같으면 skuId)으로 한 페이지씩 돌려줍니다. "
        "date 를 생략하면 그 고객사의 가장 최근 적재 일자이고, 응답의 inventoryDate 가 실제 기준 일자입니다. "
        "date 는 하루치 스냅샷을 고르는 날짜(YYYY-MM-DD)라 기간 필터 규칙(명세 C18, UTC 시각)의 대상이 아닙니다. "
        "onHandQuantity = reservedQuantity + availableQuantity 이고 reserved 에는 다음 날 출고 대기분이 들어 있습니다(BR-003). "
        "page·size 로 페이지를 나누고(기본 20, 최대 100), totalCount·totalPages 는 모든 필터를 적용한 값입니다. "
        "고객사 범위는 access_token 쿠키의 company_id 입니다. "
        "검증 오류로 거부된 레코드는 이 목록에 없습니다(GET /validation-issues 몫)."
    ),
    response_model=InventoryListResponse,
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
                "쿼리 파라미터 형식 오류 (날짜 형식, UUID 형식, page < 1, size 범위 밖, q 길이 초과)"
            ),
        },
    },
)
async def list_inventory(
    inventory_date: IsoDate | None = Query(
        default=None,
        alias="date",
        description="재고 기준 일자 (YYYY-MM-DD). 생략하면 고객사의 최신 적재 일자",
    ),
    warehouse_id: uuid.UUID | None = Query(
        default=None, alias="warehouseId", description="창고 ID"
    ),
    sku_id: uuid.UUID | None = Query(
        default=None, alias="skuId", description="상품 ID"
    ),
    q: Annotated[
        SearchText,
        Query(
            max_length=MAX_QUERY_LENGTH,
            description="SKU 코드 또는 상품명 일부 (대소문자 무시)",
        ),
    ] = None,
    page: PageParams = Depends(),
    tenant: TenantContext = Depends(get_tenant_context),
    service: InventoryListService = Depends(get_inventory_list_service),
) -> InventoryListResponse:
    """재고 목록 조회 API 엔드포인트"""
    result = await service.get_page(
        tenant,
        inventory_date=inventory_date,
        warehouse_id=warehouse_id,
        sku_id=sku_id,
        query=q,
        page=page,
    )
    return InventoryListResponse.from_result(result)
