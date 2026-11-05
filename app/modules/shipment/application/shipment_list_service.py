import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from app.core.pagination import PageParams
from app.core.security.tenant import TenantContext
from app.modules.delay_analysis.application.delay_display import (
    ShipmentLineQuantity,
    build_route,
    calculate_delay_days,
    count_skus,
    pick_representative_line,
)
from app.modules.shipment.infrastructure.repository.shipment_repository import (
    ShipmentFilter,
    ShipmentLineRow,
    ShipmentListRow,
    ShipmentRepository,
)

"""
    선적 목록 서비스 (API 명세 v6.2 4.3 GET /shipments, 연동 데이터 조회 화면의 선적 탭)
      - 고객사 범위는 토큰의 company_id(TenantContext)다. RLS 컨텍스트는 get_tenant_context 가 설정해 둔다.
      - 명세 4.3 필드에 지연 목록과 같은 표시값(route·representativeSku·skuCount·delayDays)을 더한다.
        명세 4.5 가 선적 표시값을 "지연 목록과 같은 계산 함수(D13)"로 만들라고 하므로
        delay_analysis 모듈의 delay_display 함수를 그대로 쓴다 (응답 본문 규칙 7절: 필드 추가는 자유).
      - 지연 판정(isDelayed·delayDays)의 기준값은 입고 전이면 예상 입고일, 입고 완료면 실제 입고일이다
        (서비스 플로우 1절: 입고 전은 "지연 위험 감지", 입고 후는 "사후 성과 평가").
        RECEIVED ⇔ 실제 입고일 있음(shipments_ck3)이므로 coalesce(actual, expected) 로 두 경우를 덮는다.
        isDelayed = delayDays > 0 이고, 회복되어 기준값 ≤ 계획일이면 delayDays 는 0 이다.
      - 상세 줄(대표 상품·품목 수)은 페이지의 선적 ID 로 한 번에 읽어 선적별로 묶는다 (페이지당 SQL 1회 추가).
"""

# 경로 출발지는 한국 출고 창고의 지역이다. 고객사에 KR_ORIGIN 창고가 없으면 이 값을 쓴다 (지연 목록과 같음).
DEFAULT_ORIGIN_REGION = "KR"


@dataclass(frozen=True)
class ShipmentListItem:
    """선적 탭의 한 행. 응답 스키마와 같은 필드 이름이다. 대표 상품은 상세 줄이 없으면 None 이다."""

    shipment_id: uuid.UUID
    shipment_code: str
    transport_mode: str
    route: str
    warehouse_id: uuid.UUID
    warehouse_code: str
    region_code: str
    representative_sku_id: uuid.UUID | None
    representative_sku_code: str | None
    representative_product_name: str | None
    sku_count: int
    stage: str
    planned_kr_release_date: date
    planned_3pl_inbound_date: date
    expected_3pl_inbound_date: date
    actual_3pl_inbound_date: date | None
    is_delayed: bool
    delay_days: int


@dataclass(frozen=True)
class ShipmentListResult:
    items: list[ShipmentListItem]
    total_count: int
    page: PageParams


class ShipmentListService:
    """연동 데이터 조회 화면의 선적 탭 한 페이지를 만든다."""

    def __init__(self, repo: ShipmentRepository):
        self.repo = repo

    async def get_page(
        self,
        tenant: TenantContext,
        *,
        stage: str | None = None,
        transport_mode: str | None = None,
        warehouse_id: uuid.UUID | None = None,
        query: str | None = None,
        page: PageParams | None = None,
    ) -> ShipmentListResult:
        """page 를 생략하면 첫 페이지(기본 크기)다. 검색어는 양끝 공백을 지우고 비면 없는 것으로 본다."""
        page = page or PageParams()
        filters = ShipmentFilter(
            stage=stage,
            transport_mode=transport_mode,
            warehouse_id=warehouse_id,
            query=query.strip() or None if query else None,
        )
        repo_page = await self.repo.find_page(
            tenant.company_id, filters, offset=page.offset, limit=page.limit
        )
        lines = _group_lines(
            await self.repo.find_lines(
                tenant.company_id, [row.shipment_id for row in repo_page.rows]
            )
        )
        return ShipmentListResult(
            items=[
                self._build_item(row, lines[row.shipment_id]) for row in repo_page.rows
            ],
            total_count=repo_page.total_count,
            page=page,
        )

    @staticmethod
    def _build_item(
        row: ShipmentListRow, lines: list[ShipmentLineRow]
    ) -> ShipmentListItem:
        representative = _representative(lines)
        delay_days = calculate_delay_days(
            row.planned_3pl_inbound_date,
            row.actual_3pl_inbound_date or row.expected_3pl_inbound_date,
        )
        return ShipmentListItem(
            shipment_id=row.shipment_id,
            shipment_code=row.shipment_code,
            transport_mode=row.transport_mode,
            route=build_route(
                row.origin_region_code or DEFAULT_ORIGIN_REGION, row.us_port_name
            ),
            warehouse_id=row.warehouse_id,
            warehouse_code=row.warehouse_code,
            region_code=row.region_code,
            representative_sku_id=representative.sku_id if representative else None,
            representative_sku_code=(
                representative.sku_code if representative else None
            ),
            representative_product_name=(
                representative.product_name if representative else None
            ),
            sku_count=count_skus(_quantities(lines)),
            stage=row.stage,
            planned_kr_release_date=row.planned_kr_release_date,
            planned_3pl_inbound_date=row.planned_3pl_inbound_date,
            expected_3pl_inbound_date=row.expected_3pl_inbound_date,
            actual_3pl_inbound_date=row.actual_3pl_inbound_date,
            is_delayed=delay_days > 0,
            delay_days=delay_days,
        )


def _quantities(lines: list[ShipmentLineRow]) -> list[ShipmentLineQuantity]:
    return [
        ShipmentLineQuantity(
            line_no=line.line_no,
            sku_id=line.sku_id,
            shipped_quantity=line.shipped_quantity,
        )
        for line in lines
    ]


def _representative(lines: list[ShipmentLineRow]) -> ShipmentLineRow | None:
    """수량이 가장 많은 상세 줄(같으면 줄 번호가 작은 줄). 상세가 없으면 None (D13, 지연 목록과 같은 규칙)."""
    picked = pick_representative_line(_quantities(lines))
    if picked is None:
        return None
    return next(line for line in lines if line.line_no == picked.line_no)


def _group_lines(
    lines: list[ShipmentLineRow],
) -> defaultdict[uuid.UUID, list[ShipmentLineRow]]:
    grouped: defaultdict[uuid.UUID, list[ShipmentLineRow]] = defaultdict(list)
    for line in lines:
        grouped[line.shipment_id].append(line)
    return grouped
