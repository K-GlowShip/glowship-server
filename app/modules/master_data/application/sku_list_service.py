import uuid
from dataclasses import dataclass
from enum import Enum

from app.core.pagination import PageParams
from app.core.security.tenant import TenantContext
from app.modules.master_data.infrastructure.repository.sku_repository import (
    ExternalCodeRow,
    SkuListRow,
    SkuRepository,
)


class SkuValidationStatus(str, Enum):
    """
    상품 한 건의 검증 상태 (API 명세 4.3 GET /skus 의 validationStatus)
      - CODE_MISSING: 원천 중 MAPPED 가 아닌 매핑이 있음
      - ISSUE_OPEN: 이 SKU 코드로 열린 검증 오류가 있음
      - OK: 그 밖
    둘 다 해당하면 CODE_MISSING 이다 (명세의 나열 순서).
    """

    OK = "OK"
    CODE_MISSING = "CODE_MISSING"
    ISSUE_OPEN = "ISSUE_OPEN"


@dataclass(frozen=True)
class SkuListItem:
    """상품 탭의 한 행. 응답 스키마와 같은 필드 이름이다."""

    sku_id: uuid.UUID
    sku_code: str
    product_name: str
    brand_name: str
    category: str
    subcategory: str
    popularity_tier: str
    is_emergency_air_eligible: bool
    unit_code: str
    units_per_case: int
    status: str
    external_codes: tuple[ExternalCodeRow, ...]
    validation_status: SkuValidationStatus


@dataclass(frozen=True)
class SkuListResult:
    items: list[SkuListItem]
    total_count: int
    page: PageParams


class SkuListService:
    """연동 데이터 조회 화면의 상품 탭 한 페이지를 만든다."""

    def __init__(self, repo: SkuRepository):
        self.repo = repo

    async def get_page(
        self,
        tenant: TenantContext,
        *,
        query: str | None = None,
        only_errors: bool = False,
        page: PageParams | None = None,
    ) -> SkuListResult:
        """page 를 생략하면 첫 페이지(기본 크기)다. 검색어는 양끝 공백을 지우고 비면 없는 것으로 본다."""
        page = page or PageParams()
        repo_page = await self.repo.find_page(
            tenant.company_id,
            query=query.strip() or None if query else None,
            only_errors=only_errors,
            offset=page.offset,
            limit=page.limit,
        )
        return SkuListResult(
            items=[self._build_item(row) for row in repo_page.rows],
            total_count=repo_page.total_count,
            page=page,
        )

    @staticmethod
    def _build_item(row: SkuListRow) -> SkuListItem:
        return SkuListItem(
            sku_id=row.sku_id,
            sku_code=row.sku_code,
            product_name=row.product_name,
            brand_name=row.brand_name,
            category=row.category,
            subcategory=row.subcategory,
            popularity_tier=row.popularity_tier,
            is_emergency_air_eligible=row.emergency_air_eligible,
            unit_code=row.unit_code,
            units_per_case=row.units_per_case,
            status=row.status,
            external_codes=row.external_codes,
            validation_status=SkuListService._validation_status(row),
        )

    @staticmethod
    def _validation_status(row: SkuListRow) -> SkuValidationStatus:
        if row.has_unmapped:
            return SkuValidationStatus.CODE_MISSING
        if row.has_open_issue:
            return SkuValidationStatus.ISSUE_OPEN
        return SkuValidationStatus.OK
