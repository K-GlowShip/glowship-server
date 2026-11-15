import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.exceptions import ErrorResponse
from app.core.pagination import PageParams
from app.core.security.tenant import TenantContext, get_tenant_context
from app.core.text_input import SearchText
from app.modules.shipment.application.shipment_detail_service import (
    ShipmentDetailService,
)
from app.modules.shipment.application.shipment_list_service import (
    ShipmentListService,
)
from app.modules.shipment.dependencies import (
    get_shipment_detail_service,
    get_shipment_list_service,
)
from app.modules.shipment.infrastructure.shipment_enums import (
    ShipmentStage,
    TransportMode,
)
from app.modules.shipment.presentation.shipment_schemas import (
    MAX_QUERY_LENGTH,
    ShipmentDetailResponse,
    ShipmentListResponse,
)

router = APIRouter(prefix="/api/v1/shipments", tags=["선적 (Client)"])


@router.get(
    "",
    summary="선적 목록 (API 12)",
    description=(
        "고객사의 한국 → 미국 3PL 선적을 계획 3PL 입고일 최신순(같으면 shipmentId 순)으로 한 페이지씩 돌려줍니다. "
        "행마다 도착 창고 요약(warehouse)과, 지연 목록과 같은 계산(D13)으로 만든 경로(route)·대표 상품(representativeSku)·"
        "품목 수(skuCount)·지연 일수(delayDays)가 붙습니다. "
        "stage·expected3plInboundDate 는 선적의 현재 값입니다. "
        "isDelayed 는 입고 전이면 예상 입고일, 입고 완료면 실제 입고일이 계획 3PL 입고일보다 늦은지이고 delayDays 는 그 일수입니다. "
        "page·size 로 페이지를 나누고(기본 20, 최대 100), totalCount·totalPages 는 모든 필터를 적용한 값입니다. "
        "고객사 범위는 access_token 쿠키의 company_id 입니다. "
        "검증 오류로 거부된 선적은 이 목록에 없습니다(GET /validation-issues 몫). "
        "상세 줄·계획 근거·일정 변경 이력은 GET /shipments/{shipmentId} 몫입니다."
    ),
    response_model=ShipmentListResponse,
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
                "쿼리 파라미터 형식 오류 (허용되지 않은 코드값, UUID 형식, page < 1, size 범위 밖, q 길이 초과)"
            ),
        },
    },
)
async def list_shipments(
    stage: ShipmentStage | None = Query(
        default=None,
        description=(
            "현재 운송 단계: PRE_DEPARTURE(출항 전)·AT_SEA(해상 운송)·IN_AIR(항공 운송)·"
            "AT_PORT(항구 도착·통관 중)·RECEIVED(입고 완료)"
        ),
    ),
    transport_mode: TransportMode | None = Query(
        default=None, alias="transportMode", description="운송 수단: OCEAN·AIR"
    ),
    warehouse_id: uuid.UUID | None = Query(
        default=None, alias="warehouseId", description="도착 창고 ID"
    ),
    q: Annotated[
        SearchText,
        Query(
            max_length=MAX_QUERY_LENGTH,
            description="선적 코드 일부 (대소문자 무시)",
        ),
    ] = None,
    page: PageParams = Depends(),
    tenant: TenantContext = Depends(get_tenant_context),
    service: ShipmentListService = Depends(get_shipment_list_service),
) -> ShipmentListResponse:
    """선적 목록 조회 API 엔드포인트"""
    result = await service.get_page(
        tenant,
        stage=stage.value if stage else None,
        transport_mode=transport_mode.value if transport_mode else None,
        warehouse_id=warehouse_id,
        query=q,
        page=page,
    )
    return ShipmentListResponse.from_result(result)


@router.get(
    "/{shipment_id}",
    summary="선적 상세 (API 13)",
    description=(
        "선적 한 건의 상세입니다. 목록 행 필드(경로·대표 상품·품목 수·지연 일수 포함, 목록과 같은 계산 D13)에 "
        "현재 예상 항구 도착일(expectedPortArrivalDate), 상세 줄(lines: 줄 번호·상품·수량), "
        "계획 리드타임 근거(plannedBasis: 기준 달·도착 항구·총 리드타임 p50·p90·신뢰도), "
        "일정 변경 이력(scheduleHistory: 관측 시각 오래된 순, 관측 전부), "
        "데이터 기준 시각(dataBasisAt: 이 선적 행을 마지막으로 바꾼 수집의 종료 시각)이 붙습니다. "
        "항구 도착일과 3PL 입고일은 다른 날짜입니다(BR-007). "
        "고객사 범위는 access_token 쿠키의 company_id 이고, 없거나 다른 고객사 선적은 404 입니다."
    ),
    response_model=ShipmentDetailResponse,
    responses={
        401: {
            "model": ErrorResponse,
            "description": "로그인 필요 (access_token 쿠키 없음·만료)",
        },
        403: {
            "model": ErrorResponse,
            "description": "고객사 범위 없음 (OWNER·ADMIN 토큰)",
        },
        404: {
            "model": ErrorResponse,
            "description": "SHIPMENT_NOT_FOUND: 없거나 다른 고객사의 선적",
        },
        422: {
            "model": ErrorResponse,
            "description": "경로 파라미터 형식 오류 (shipmentId 가 UUID 아님)",
        },
    },
)
async def get_shipment(
    shipment_id: uuid.UUID,
    tenant: TenantContext = Depends(get_tenant_context),
    service: ShipmentDetailService = Depends(get_shipment_detail_service),
) -> ShipmentDetailResponse:
    """선적 상세 조회 API 엔드포인트"""
    return ShipmentDetailResponse.from_detail(await service.get(tenant, shipment_id))
