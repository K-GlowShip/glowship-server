import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import UUID, Boolean, Integer, Text, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

"""
    상품(SKU) 목록 조회 (API 명세 4.3 GET /skus, 연동 데이터 조회 화면의 상품 탭)
      - 상품 한 건에 원천별 외부 코드 목록과 검증 상태 판정 재료(매핑 누락·OPEN 이슈 여부)를 붙여 한 페이지를 돌려준다.
      - 외부 코드: external_entity_mappings 의 entity_type = 'SKU' 행을 sku 별로 모은다 (source_domain 순).
        창고별 SKU 코드는 제공하지 않는다 (명세 D15).
      - OPEN 이슈: data_validation_issues 에 sku_id 가 없어 INVENTORY 이슈의 source_record_key
        ('창고코드|상품코드|날짜') 둘째 토막을 sku_code 로 본다. 수집기(GLOW-13)가 sku_id 컬럼을
        채우게 되면 이 파싱을 조인으로 바꾼다. ORDER·SHIPMENT 이슈는 각 탭의 몫이라 여기서 다루지 않는다.
      - 정렬은 sku_code, 같으면 sku_id (명세 4.3, 응답 규칙 6.2).
      - 운영에서는 RLS 가 고객사 범위를 강제하지만, 조건을 명시해 로컬(superuser)에서도 같은 결과가 나오게 한다.
"""

_LIKE_ESCAPE = "\\"

_COMMON_CTES = """
    WITH external_codes AS (
        SELECT sku_id,
               jsonb_agg(
                   jsonb_build_object(
                       'source_domain', source_domain,
                       'external_code', external_code,
                       'mapping_status', mapping_status
                   )
                   ORDER BY source_domain, external_code
               ) AS codes,
               BOOL_OR(mapping_status <> 'MAPPED') AS has_unmapped
          FROM external_entity_mappings
         WHERE company_id = :company_id AND entity_type = 'SKU'
         GROUP BY sku_id
    ),
    open_issue_skus AS (
        SELECT DISTINCT split_part(source_record_key, '|', 2) AS sku_code
          FROM data_validation_issues
         WHERE company_id = :company_id
           AND status = 'OPEN'
           AND source_entity_type = 'INVENTORY'
    ),
    filtered AS (
        SELECT s.sku_id, s.sku_code, s.product_name, s.brand_name, s.category, s.subcategory,
               s.popularity_tier, s.emergency_air_eligible, s.unit_code, s.units_per_case, s.status,
               COALESCE(c.codes, '[]'::jsonb) AS external_codes,
               COALESCE(c.has_unmapped, false) AS has_unmapped,
               (i.sku_code IS NOT NULL) AS has_open_issue
          FROM skus s
          LEFT JOIN external_codes c ON c.sku_id = s.sku_id
          LEFT JOIN open_issue_skus i ON i.sku_code = s.sku_code
         WHERE s.company_id = :company_id
           AND (:pattern IS NULL
                OR s.product_name ILIKE :pattern ESCAPE '\\'
                OR s.sku_code ILIKE :pattern ESCAPE '\\')
           AND (NOT :only_errors OR c.has_unmapped OR i.sku_code IS NOT NULL)
    )
"""

_LIST_PAGE = text(
    _COMMON_CTES
    + """
    SELECT sku_id, sku_code, product_name, brand_name, category, subcategory,
           popularity_tier, emergency_air_eligible, unit_code, units_per_case, status,
           external_codes, has_unmapped, has_open_issue
      FROM filtered
     ORDER BY sku_code, sku_id
    OFFSET :offset LIMIT :limit
    """
).bindparams(
    bindparam("company_id", type_=UUID(as_uuid=True)),
    bindparam("pattern", type_=Text()),
    bindparam("only_errors", type_=Boolean()),
    bindparam("offset", type_=Integer()),
    bindparam("limit", type_=Integer()),
)

# 페이지가 범위를 벗어나도 totalCount 를 알 수 있도록 건수는 별도 문장으로 센다.
_COUNT_PAGE = text(
    _COMMON_CTES + "SELECT COUNT(*) AS total_count FROM filtered"
).bindparams(
    bindparam("company_id", type_=UUID(as_uuid=True)),
    bindparam("pattern", type_=Text()),
    bindparam("only_errors", type_=Boolean()),
)


@dataclass(frozen=True)
class ExternalCodeRow:
    """원천 시스템 하나가 이 상품을 부르는 코드"""

    source_domain: str
    external_code: str
    mapping_status: str


@dataclass(frozen=True)
class SkuListRow:
    """상품 한 건과 검증 상태 판정 재료. 매핑이 없으면 external_codes 는 빈 목록이다."""

    sku_id: uuid.UUID
    sku_code: str
    product_name: str
    brand_name: str
    category: str
    subcategory: str
    popularity_tier: str
    emergency_air_eligible: bool
    unit_code: str
    units_per_case: int
    status: str
    external_codes: tuple[ExternalCodeRow, ...]
    has_unmapped: bool
    has_open_issue: bool


@dataclass(frozen=True)
class SkuListPage:
    rows: list[SkuListRow]
    total_count: int


def _like_pattern(query: str | None) -> str | None:
    """부분 일치 패턴. 사용자 입력의 LIKE 특수문자(\\ % _)는 글자 그대로 찾도록 이스케이프한다."""
    if query is None:
        return None
    escaped = (
        query.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", _LIKE_ESCAPE + "%")
        .replace("_", _LIKE_ESCAPE + "_")
    )
    return f"%{escaped}%"


def _external_codes(
    value: list[dict[str, Any]] | str | None,
) -> tuple[ExternalCodeRow, ...]:
    """jsonb_agg 결과 → ExternalCodeRow 튜플. asyncpg 는 list[dict] 로, 코덱이 없는 드라이버는 문자열로 준다."""
    if isinstance(value, str):
        value = json.loads(value)
    return tuple(
        ExternalCodeRow(
            source_domain=item["source_domain"],
            external_code=item["external_code"],
            mapping_status=item["mapping_status"],
        )
        for item in (value or [])
    )


class SkuRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # Read
    async def find_page(
        self,
        company_id: uuid.UUID,
        *,
        query: str | None,
        only_errors: bool,
        offset: int,
        limit: int,
    ) -> SkuListPage:
        """고객사의 상품 한 페이지(sku_code·sku_id 순)와 같은 필터를 적용한 전체 건수."""
        filters = {
            "company_id": company_id,
            "pattern": _like_pattern(query),
            "only_errors": only_errors,
        }
        rows = await self.db.execute(
            _LIST_PAGE, {**filters, "offset": offset, "limit": limit}
        )
        total_count = await self.db.scalar(_COUNT_PAGE, filters)
        return SkuListPage(
            rows=[
                SkuListRow(
                    sku_id=row.sku_id,
                    sku_code=row.sku_code,
                    product_name=row.product_name,
                    brand_name=row.brand_name,
                    category=row.category,
                    subcategory=row.subcategory,
                    popularity_tier=row.popularity_tier,
                    emergency_air_eligible=row.emergency_air_eligible,
                    unit_code=row.unit_code,
                    units_per_case=row.units_per_case,
                    status=row.status,
                    external_codes=_external_codes(row.external_codes),
                    has_unmapped=row.has_unmapped,
                    has_open_issue=row.has_open_issue,
                )
                for row in rows
            ],
            total_count=total_count or 0,
        )
