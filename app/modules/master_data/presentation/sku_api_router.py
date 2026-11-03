from fastapi import APIRouter, Depends, Query

from app.core.exceptions import ErrorResponse
from app.core.pagination import PageParams
from app.core.security.tenant import TenantContext, get_tenant_context
from app.modules.master_data.application.sku_list_service import SkuListService
from app.modules.master_data.dependencies import get_sku_list_service
from app.modules.master_data.presentation.sku_schemas import (
    MAX_QUERY_LENGTH,
    SkuListResponse,
)

router = APIRouter(prefix="/api/v1/skus", tags=["상품 마스터 (Client)"])


@router.get(
    "",
    summary="상품 목록 (API 8)",
    description=(
        "고객사의 상품(SKU)을 skuCode 순(같으면 skuId)으로 한 페이지씩 돌려줍니다. "
        "상품마다 원천별 외부 코드(externalCodes)와 검증 상태(validationStatus)가 붙습니다. "
        "page·size 로 페이지를 나누고(기본 20, 최대 100), totalCount·totalPages 는 모든 필터를 적용한 값입니다. "
        "고객사 범위는 access_token 쿠키의 company_id 입니다."
    ),
    response_model=SkuListResponse,
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
            "description": "쿼리 파라미터 형식 오류 (page < 1, size 범위 밖, q 길이 초과)",
        },
    },
)
async def list_skus(
    q: str | None = Query(
        default=None,
        max_length=MAX_QUERY_LENGTH,
        description="상품명 또는 ERP 코드 일부 (대소문자 무시)",
    ),
    only_errors: bool = Query(
        default=False,
        alias="onlyErrors",
        description="검증 상태가 OK 가 아닌 상품만 (CODE_MISSING·ISSUE_OPEN)",
    ),
    page: PageParams = Depends(),
    tenant: TenantContext = Depends(get_tenant_context),
    service: SkuListService = Depends(get_sku_list_service),
) -> SkuListResponse:
    """상품 목록 조회 API 엔드포인트"""
    result = await service.get_page(tenant, query=q, only_errors=only_errors, page=page)
    return SkuListResponse.from_result(result)
