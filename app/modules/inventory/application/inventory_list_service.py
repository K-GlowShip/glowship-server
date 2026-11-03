import uuid
from dataclasses import dataclass
from datetime import date

from app.core.pagination import PageParams
from app.core.security.tenant import TenantContext
from app.modules.inventory.infrastructure.repository.inventory_repository import (
    InventoryFilter,
    InventoryRepository,
    InventoryRow,
)

"""
    재고 목록 서비스 (API 명세 v6.2 4.3 GET /inventory, 연동 데이터 조회 화면의 재고 탭)
      - 고객사 범위는 토큰의 company_id(TenantContext)다. RLS 컨텍스트는 get_tenant_context 가 설정해 둔다.
      - 조회 일자(date)를 생략하면 그 고객사의 가장 최근 적재 일자를 쓴다 (오늘이 아니다). 적재된 재고가 없으면
        DB 를 더 읽지 않고 빈 결과(inventory_date = None)를 돌려준다.
      - date 는 하루치 스냅샷을 고르는 날짜(YYYY-MM-DD)다. 기간 필터(from·to)가 아니라 명세 C18(UTC 시각)의 대상이 아니다.
"""


@dataclass(frozen=True)
class InventoryListItem:
    """재고 탭의 한 행. 응답 스키마와 같은 필드 이름이다."""

    inventory_date: date
    warehouse_id: uuid.UUID
    warehouse_code: str
    region_code: str
    sku_id: uuid.UUID
    sku_code: str
    product_name: str
    on_hand_quantity: int
    reserved_quantity: int
    available_quantity: int
    outbound_quantity: int
    data_sync_run_id: uuid.UUID


@dataclass(frozen=True)
class InventoryListResult:
    items: list[InventoryListItem]
    total_count: int
    page: PageParams
    inventory_date: date | None


class InventoryListService:
    """연동 데이터 조회 화면의 재고 탭 한 페이지를 만든다."""

    def __init__(self, repo: InventoryRepository):
        self.repo = repo

    async def get_page(
        self,
        tenant: TenantContext,
        *,
        inventory_date: date | None = None,
        warehouse_id: uuid.UUID | None = None,
        sku_id: uuid.UUID | None = None,
        query: str | None = None,
        page: PageParams | None = None,
    ) -> InventoryListResult:
        """page 를 생략하면 첫 페이지(기본 크기)다. 검색어는 양끝 공백을 지우고 비면 없는 것으로 본다."""
        page = page or PageParams()
        if inventory_date is None:
            inventory_date = await self.repo.find_latest_date(tenant.company_id)
            if inventory_date is None:
                return InventoryListResult(
                    items=[], total_count=0, page=page, inventory_date=None
                )
        filters = InventoryFilter(
            inventory_date=inventory_date,
            warehouse_id=warehouse_id,
            sku_id=sku_id,
            query=query.strip() or None if query else None,
        )
        repo_page = await self.repo.find_page(
            tenant.company_id, filters, offset=page.offset, limit=page.limit
        )
        return InventoryListResult(
            items=[self._build_item(row) for row in repo_page.rows],
            total_count=repo_page.total_count,
            page=page,
            inventory_date=inventory_date,
        )

    @staticmethod
    def _build_item(row: InventoryRow) -> InventoryListItem:
        return InventoryListItem(
            inventory_date=row.inventory_date,
            warehouse_id=row.warehouse_id,
            warehouse_code=row.warehouse_code,
            region_code=row.region_code,
            sku_id=row.sku_id,
            sku_code=row.sku_code,
            product_name=row.product_name,
            on_hand_quantity=row.on_hand_quantity,
            reserved_quantity=row.reserved_quantity,
            available_quantity=row.available_quantity,
            outbound_quantity=row.outbound_quantity,
            data_sync_run_id=row.data_sync_run_id,
        )
