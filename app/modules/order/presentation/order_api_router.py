import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query

from app.core.exceptions import ErrorResponse
from app.core.pagination import PageParams
from app.core.security.tenant import TenantContext, get_tenant_context
from app.modules.order.application.order_list_service import OrderListService
from app.modules.order.dependencies import get_order_list_service
from app.modules.order.infrastructure.order_enums import OrderStatus, SalesChannel
from app.modules.order.presentation.order_schemas import (
    MAX_QUERY_LENGTH,
    OrderListResponse,
)

router = APIRouter(prefix="/api/v1/orders", tags=["주문 (Client)"])


@router.get(
    "",
    summary="주문 목록 (API 10)",
    description=(
        "고객사의 주문을 주문 일시 최신순(같으면 orderId 순)으로 한 페이지씩 돌려줍니다. "
        "행마다 출고 창고 요약(warehouse)과 주문 상세(lines[], SKU 요약 포함)가 붙습니다. "
        "주문 기간(from·to)은 ISO 8601 UTC 시각이며 범위는 [from, to) 입니다(to 미포함). "
        "page·size 로 페이지를 나누고(기본 20, 최대 100), totalCount·totalPages 는 모든 필터를 적용한 값입니다. "
        "고객사 범위는 access_token 쿠키의 company_id 입니다. "
        "검증 오류로 거부된 주문은 이 목록에 없습니다(GET /validation-issues 몫)."
    ),
    response_model=OrderListResponse,
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
                "쿼리 파라미터 형식 오류 (허용되지 않은 코드값, 시각 형식, from > to, "
                "page < 1, size 범위 밖, q 길이 초과)"
            ),
        },
    },
)
async def list_orders(
    status: OrderStatus | None = Query(
        default=None,
        description=(
            "주문 상태: OPEN(미출고)·PARTIALLY_SHIPPED(부분 출고)·SHIPPED(출고 완료)·"
            "DELIVERED(배송 완료)·CANCELLED(취소)·REMOVED_AT_SOURCE(원천 삭제)"
        ),
    ),
    warehouse_id: uuid.UUID | None = Query(
        default=None, alias="warehouseId", description="출고 창고 ID"
    ),
    sales_channel: SalesChannel | None = Query(
        default=None, alias="salesChannel", description="판매 채널: AMAZON·SEPHORA"
    ),
    ordered_from: datetime | None = Query(
        default=None,
        alias="from",
        description="주문 일시 시작 (ISO 8601 UTC 시각, 포함). 예: 2026-06-30T15:00:00Z",
    ),
    ordered_to: datetime | None = Query(
        default=None,
        alias="to",
        description="주문 일시 끝 (ISO 8601 UTC 시각, 미포함). 예: 2026-07-01T15:00:00Z",
    ),
    q: str | None = Query(
        default=None,
        max_length=MAX_QUERY_LENGTH,
        description="주문 번호 또는 주문 상품의 SKU 코드 일부 (대소문자 무시)",
    ),
    page: PageParams = Depends(),
    tenant: TenantContext = Depends(get_tenant_context),
    service: OrderListService = Depends(get_order_list_service),
) -> OrderListResponse:
    """주문 목록 조회 API 엔드포인트"""
    result = await service.get_page(
        tenant,
        status=status.value if status else None,
        warehouse_id=warehouse_id,
        sales_channel=sales_channel.value if sales_channel else None,
        ordered_from=ordered_from,
        ordered_to=ordered_to,
        query=q,
        page=page,
    )
    return OrderListResponse.from_result(result)
