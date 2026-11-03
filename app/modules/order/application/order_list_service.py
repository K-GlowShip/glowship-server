import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime

from app.core.pagination import PageParams
from app.core.security.tenant import TenantContext
from app.modules.order.application.exception.order_exceptions import (
    InvalidOrderedPeriodException,
)
from app.modules.order.infrastructure.repository.order_repository import (
    OrderFilter,
    OrderListRow,
    OrderRepository,
)

"""
    주문 목록 서비스 (API 명세 v6.2 4.3 GET /orders, 연동 데이터 조회 화면의 주문 탭)
      - 고객사 범위는 토큰의 company_id(TenantContext)다. RLS 컨텍스트는 get_tenant_context 가 설정해 둔다.
      - 주문 기간(from·to)은 ISO 8601 UTC 시각이고 범위는 [from, to) 다 (to 미포함, 명세 v6.2 C18·C20).
        시간대가 있는 값만 들어온다(시간대 없는 값·날짜만은 라우터가 422 로 거부). 다른 시간대 값은 UTC 로 맞춘다.
      - 주문 상세 줄(lines[])은 보류했다(2026-11-03). 명세 4.3 에는 있으나 화면 확정 뒤 다시 넣는다.
      - 출고 관련 날짜 4개(명세 D8)는 저장된 열이 아니라 서비스가 계산한다 (명세 4.3 저장 위치).
        actualDispatchDate 는 리포지토리가 data_change_records 로 역산한다 (첫 ORDER_LINE UPDATE 수집일 − 1, 참조 엔진 규칙).
        securedDate·plannedDispatchDate·expectedDeliveryDate 는 참조 엔진의 배정 재생(snapshot.py)과 리드타임 계산이 필요해
        아직 계산하지 않고 null 로 내린다 (별도 작업). 응답 모양(키 4개)은 명세대로 유지한다.
"""


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
        ordered_from, ordered_to = _to_utc(ordered_from), _to_utc(ordered_to)
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
        return OrderListResult(
            items=[self._build_item(row) for row in repo_page.rows],
            total_count=repo_page.total_count,
            page=page,
        )

    @staticmethod
    def _build_item(row: OrderListRow) -> OrderListItem:
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
            # 배정 재생이 필요한 세 값은 아직 계산하지 않는다 (null). actualDispatchDate 만 역산 값이다.
            secured_date=None,
            planned_dispatch_date=None,
            actual_dispatch_date=row.actual_dispatch_date,
            expected_delivery_date=None,
        )


def _to_utc(moment: datetime | None) -> datetime | None:
    """시간대가 있는 시각을 UTC 로 맞춘다 (시간대 없는 값은 라우터가 거부해 들어오지 않는다)."""
    return moment.astimezone(UTC) if moment else None
