import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime

from app.core.pagination import PageParams
from app.core.security.tenant import TenantContext
from app.modules.order.application.exception.order_exceptions import (
    InvalidOrderedPeriodException,
)
from app.modules.order.infrastructure.repository.order_repository import (
    OrderFilter,
    OrderLineRow,
    OrderListRow,
    OrderRepository,
)

"""
    주문 목록 서비스 (API 명세 v6.2 4.3 GET /orders, 연동 데이터 조회 화면의 주문 탭)
      - 고객사 범위는 토큰의 company_id(TenantContext)다. RLS 컨텍스트는 get_tenant_context 가 설정해 둔다.
      - 주문 기간(from·to)은 ISO 8601 UTC 시각이고 범위는 [from, to) 다 (to 미포함, 명세 변경 2026-11-03).
        KST → UTC 변환은 프론트가 한다. 시간대 표시가 없는 값은 UTC 로 보고, 다른 시간대 값은 UTC 로 맞춘다.
      - 상세 줄은 페이지의 주문 ID 로 한 번에 읽어 주문별로 묶는다.
      - 출고 관련 날짜 4개(securedDate 등)는 orders 열(마이그레이션 0005_orders)을 그대로 내린다 (D8, ERP 원천에서 받음).
        수집이 채우기 전에는 null 이다.
"""


@dataclass(frozen=True)
class OrderLineItem:
    """주문 상세 한 줄. 응답 스키마와 같은 필드 이름이다."""

    line_no: int
    sku_id: uuid.UUID
    sku_code: str
    product_name: str
    ordered_quantity: int
    shipped_quantity: int
    line_status: str


@dataclass(frozen=True)
class OrderListItem:
    """주문 탭의 한 행. 응답 스키마와 같은 필드 이름이다."""

    order_id: uuid.UUID
    order_code: str
    sales_channel: str
    customer_region: str
    warehouse_id: uuid.UUID
    warehouse_code: str
    region_code: str
    ordered_at: datetime
    promised_delivery_at: datetime
    status: str
    secured_date: date | None
    planned_dispatch_date: date | None
    actual_dispatch_date: date | None
    expected_delivery_date: date | None
    lines: tuple[OrderLineItem, ...]


@dataclass(frozen=True)
class OrderListResult:
    items: list[OrderListItem]
    total_count: int
    page: PageParams


class OrderListService:
    """연동 데이터 조회 화면의 주문 탭 한 페이지를 만든다."""

    def __init__(self, repo: OrderRepository):
        self.repo = repo

    async def get_page(
        self,
        tenant: TenantContext,
        *,
        status: str | None = None,
        warehouse_id: uuid.UUID | None = None,
        sales_channel: str | None = None,
        ordered_from: datetime | None = None,
        ordered_to: datetime | None = None,
        query: str | None = None,
        page: PageParams | None = None,
    ) -> OrderListResult:
        """page 를 생략하면 첫 페이지(기본 크기)다. 검색어는 양끝 공백을 지우고 비면 없는 것으로 본다.
        from 이 to 보다 늦으면 InvalidOrderedPeriodException(422)."""
        ordered_from, ordered_to = _as_utc(ordered_from), _as_utc(ordered_to)
        if ordered_from and ordered_to and ordered_from > ordered_to:
            raise InvalidOrderedPeriodException()
        page = page or PageParams()
        filters = OrderFilter(
            status=status,
            warehouse_id=warehouse_id,
            sales_channel=sales_channel,
            ordered_from=ordered_from,
            ordered_to=ordered_to,
            query=query.strip() or None if query else None,
        )
        repo_page = await self.repo.find_page(
            tenant.company_id, filters, offset=page.offset, limit=page.limit
        )
        lines = _group_lines(
            await self.repo.find_lines(
                tenant.company_id, [row.order_id for row in repo_page.rows]
            )
        )
        return OrderListResult(
            items=[
                self._build_item(row, lines[row.order_id]) for row in repo_page.rows
            ],
            total_count=repo_page.total_count,
            page=page,
        )

    @staticmethod
    def _build_item(row: OrderListRow, lines: list[OrderLineRow]) -> OrderListItem:
        return OrderListItem(
            order_id=row.order_id,
            order_code=row.order_code,
            sales_channel=row.sales_channel,
            customer_region=row.customer_region,
            warehouse_id=row.warehouse_id,
            warehouse_code=row.warehouse_code,
            region_code=row.region_code,
            ordered_at=row.ordered_at,
            promised_delivery_at=row.promised_delivery_at,
            status=row.status,
            secured_date=row.secured_date,
            planned_dispatch_date=row.planned_dispatch_date,
            actual_dispatch_date=row.actual_dispatch_date,
            expected_delivery_date=row.expected_delivery_date,
            lines=tuple(
                OrderLineItem(
                    line_no=line.line_no,
                    sku_id=line.sku_id,
                    sku_code=line.sku_code,
                    product_name=line.product_name,
                    ordered_quantity=line.ordered_quantity,
                    shipped_quantity=line.shipped_quantity,
                    line_status=line.line_status,
                )
                for line in lines
            ),
        )


def _as_utc(moment: datetime | None) -> datetime | None:
    """시간대 표시가 없으면 UTC 로 보고, 있으면 UTC 로 바꾼다."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _group_lines(
    lines: list[OrderLineRow],
) -> defaultdict[uuid.UUID, list[OrderLineRow]]:
    grouped: defaultdict[uuid.UUID, list[OrderLineRow]] = defaultdict(list)
    for line in lines:
        grouped[line.order_id].append(line)
    return grouped
