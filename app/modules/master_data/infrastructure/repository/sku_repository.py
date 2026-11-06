import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import UUID, Boolean, Integer, Text, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.master_data.infrastructure.master_data_enums import MappingStatus
from app.modules.master_data.infrastructure.sku_code_sources import (
    SKU_CODE_SOURCE_DOMAINS,
)

"""
    상품(SKU) 목록 조회 (API 명세 4.3 GET /skus, 연동 데이터 조회 화면의 상품 탭)
      - 상품 한 건에 원천별 외부 코드 목록과 검증 상태 판정 재료(매핑 누락·OPEN 이슈 여부)를 붙여 한 페이지를 돌려준다.
      - 외부 코드: SKU 코드를 쓰는 원천(SKU_CODE_SOURCE_DOMAINS, INVENTORY·SHIPMENT)마다 항목 하나를 항상 만든다.
        external_entity_mappings 의 MAPPED 행(entity_type = 'SKU')이 있으면 그 코드, 없으면 external_code = NULL,
        mapping_status = 'UNMAPPED' 다. MAPPED 가 아닌 행은 sku_id 가 비어 있어(DDL ck5) SKU 에 붙을 수 없으므로,
        매핑 누락(has_unmapped)은 MAPPED 행의 부재로 판정한다. 창고별 SKU 코드는 제공하지 않는다 (명세 D15).
      - OPEN 이슈: data_validation_issues 에 sku_id 가 없어 INVENTORY 이슈의 source_record_key
        ('창고코드|상품코드|날짜') 둘째 토막을 sku_code 로 본다. 수집기(GLOW-13)가 sku_id 컬럼을
        채우게 되면 이 파싱을 조인으로 바꾼다. ORDER·SHIPMENT 이슈는 각 탭의 몫이라 여기서 다루지 않는다.
      - 정렬은 sku_code, 같으면 sku_id (명세 4.3, 응답 규칙 6.2).
      - 상품 상세(GET /skus/{skuId}, GLOW-136)는 목록과 같은 CTE 로 한 건을 읽어 공통 필드·판정이 목록과 어긋나지 않게 한다.
        영향 지연은 이슈 표를 거치지 않고 shipment_lines 로 찾는다. 매핑이 없는 상품은 이슈 키에 외부 코드가 들어가
        이슈에서 상품을 찾을 수 없고, 이슈가 없는 경우도 있기 때문이다. "이 상품이 포함되고 보류 중인 지연"이지
        "이 상품 때문에 보류된 지연"을 증명하지는 않는다.
      - 운영에서는 RLS 가 고객사 범위를 강제하지만, 조건을 명시해 로컬(superuser)에서도 같은 결과가 나오게 한다.
"""

_LIKE_ESCAPE = "\\"

# SKU 코드 원천을 (순서, 원천) 행으로 펼친 VALUES 목록. 값은 서버 상수(enum)라 문자열로 넣어도 안전하다.
_SKU_SOURCE_VALUES = ", ".join(
    f"({order}, '{domain.value}')"
    for order, domain in enumerate(SKU_CODE_SOURCE_DOMAINS)
)
_MISSING_MAPPING_STATUS = MappingStatus.UNMAPPED.value

_COMMON_CTES = f"""
    WITH sku_sources (source_order, source_domain) AS (
        VALUES {_SKU_SOURCE_VALUES}
    ),
    external_codes AS (
        SELECT s.sku_id,
               jsonb_agg(
                   jsonb_build_object(
                       'source_domain', src.source_domain,
                       'external_code', m.external_code,
                       'mapping_status', COALESCE(m.mapping_status, '{_MISSING_MAPPING_STATUS}')
                   )
                   ORDER BY src.source_order, m.external_code
               ) AS codes,
               BOOL_OR(m.external_code IS NULL) AS has_unmapped
          FROM skus s
         CROSS JOIN sku_sources src
          LEFT JOIN external_entity_mappings m
                 ON m.company_id = s.company_id
                AND m.sku_id = s.sku_id
                AND m.entity_type = 'SKU'
                AND m.source_domain = src.source_domain
                AND m.mapping_status = 'MAPPED'
         WHERE s.company_id = :company_id
         GROUP BY s.sku_id
    ),
    open_issue_skus AS (
        SELECT DISTINCT split_part(source_record_key, '|', 2) AS sku_code
          FROM data_validation_issues
         WHERE company_id = :company_id
           AND status = 'OPEN'
           AND source_entity_type = 'INVENTORY'
    ),
    filtered AS (
        SELECT s.sku_id, s.sku_code, s.product_name, s.brand_name, s.hs10, s.category, s.subcategory,
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

_SKU_COLUMNS = """
    SELECT sku_id, sku_code, product_name, brand_name, hs10, category, subcategory,
           popularity_tier, emergency_air_eligible, unit_code, units_per_case, status,
           external_codes, has_unmapped, has_open_issue
      FROM filtered
"""

_LIST_PAGE = text(
    _COMMON_CTES
    + _SKU_COLUMNS
    + """
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

_SKU_DETAIL = text(_COMMON_CTES + _SKU_COLUMNS + " WHERE sku_id = :sku_id").bindparams(
    bindparam("company_id", type_=UUID(as_uuid=True)),
    bindparam("pattern", type_=Text()),
    bindparam("only_errors", type_=Boolean()),
    bindparam("sku_id", type_=UUID(as_uuid=True)),
)

# GET /skus 의 ISSUE_OPEN 판정(open_issue_skus)과 같은 조건이다. 정렬은 GET /validation-issues 와 같다(명세 4.3)
_OPEN_ISSUES = text(
    """
    SELECT data_validation_issue_id, source_record_key, field_name, error_code, error_message,
           severity, detected_at
      FROM data_validation_issues
     WHERE company_id = :company_id
       AND status = 'OPEN'
       AND source_entity_type = 'INVENTORY'
       AND split_part(source_record_key, '|', 2) = :sku_code
     ORDER BY detected_at DESC, data_validation_issue_id
    """
).bindparams(
    bindparam("company_id", type_=UUID(as_uuid=True)),
    bindparam("sku_code", type_=Text()),
)

# 한 지연의 최신 실행은 1건이다. 같은 SKU 의 선적 줄이 여러 개여도 지연이 겹치지 않도록 EXISTS 로 본다
_AFFECTED_DELAY_EVENTS = text(
    """
    SELECT e.delay_event_id, s.shipment_id, s.shipment_code,
           r.analysis_run_id, r.run_code, r.status, r.result_class, r.failure_code
      FROM delay_events e
      JOIN shipments s
        ON s.company_id = e.company_id
       AND s.shipment_id = e.shipment_id
      JOIN analysis_runs r
        ON r.company_id = e.company_id
       AND r.delay_event_id = e.delay_event_id
       AND r.is_latest
     WHERE e.company_id = :company_id
       AND e.closed_at IS NULL
       AND r.status = 'ON_HOLD'
       AND EXISTS (
           SELECT 1
             FROM shipment_lines l
            WHERE l.company_id = e.company_id
              AND l.shipment_id = e.shipment_id
              AND l.sku_id = :sku_id
       )
     ORDER BY e.detected_at DESC, e.delay_event_id
    """
).bindparams(
    bindparam("company_id", type_=UUID(as_uuid=True)),
    bindparam("sku_id", type_=UUID(as_uuid=True)),
)


@dataclass(frozen=True)
class ExternalCodeRow:
    """원천 시스템 하나가 이 상품을 부르는 코드. 매핑이 없으면 external_code 는 None, mapping_status 는 UNMAPPED 다."""

    source_domain: str
    external_code: str | None
    mapping_status: str


@dataclass(frozen=True)
class SkuListRow:
    """상품 한 건과 검증 상태 판정 재료. external_codes 는 SKU 코드 원천마다 한 항목씩이다."""

    sku_id: uuid.UUID
    sku_code: str
    product_name: str
    brand_name: str
    hs10: str
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


@dataclass(frozen=True)
class SkuIssueRow:
    """이 상품 코드로 열린 재고 검증 이슈 한 건"""

    data_validation_issue_id: uuid.UUID
    source_record_key: str
    field_name: str | None
    error_code: str
    error_message: str
    severity: str
    detected_at: datetime


@dataclass(frozen=True)
class AffectedDelayEventRow:
    """이 상품이 선적에 포함되고 현재 실행이 ON_HOLD 인 열린 지연 한 건"""

    delay_event_id: uuid.UUID
    shipment_id: uuid.UUID
    shipment_code: str
    analysis_run_id: uuid.UUID
    run_code: str
    status: str
    result_class: str | None
    failure_code: str | None


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
    """jsonb_agg 결과 → ExternalCodeRow 튜플. asyncpg 는 list[dict] 로, 코덱이 없는 드라이버는 문자열로 준다.
    매핑이 없는 원천은 external_code 가 None 이다."""
    if isinstance(value, str):
        value = json.loads(value)
    return tuple(
        ExternalCodeRow(
            source_domain=item["source_domain"],
            external_code=item.get("external_code"),
            mapping_status=item["mapping_status"],
        )
        for item in (value or [])
    )


def _sku_row(row: Any) -> SkuListRow:
    return SkuListRow(
        sku_id=row.sku_id,
        sku_code=row.sku_code,
        product_name=row.product_name,
        brand_name=row.brand_name,
        hs10=row.hs10,
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
            rows=[_sku_row(row) for row in rows],
            total_count=total_count or 0,
        )

    async def find_detail(
        self, company_id: uuid.UUID, sku_id: uuid.UUID
    ) -> SkuListRow | None:
        """고객사의 상품 한 건. 없거나 다른 고객사 상품이면 None."""
        result = await self.db.execute(
            _SKU_DETAIL,
            {
                "company_id": company_id,
                "pattern": None,
                "only_errors": False,
                "sku_id": sku_id,
            },
        )
        row = result.first()
        return _sku_row(row) if row else None

    async def find_open_issues(
        self, company_id: uuid.UUID, sku_code: str
    ) -> list[SkuIssueRow]:
        """이 상품 코드로 열린 재고 검증 이슈(detected_at 최신순, 같으면 이슈 ID)."""
        rows = await self.db.execute(
            _OPEN_ISSUES, {"company_id": company_id, "sku_code": sku_code}
        )
        return [
            SkuIssueRow(
                data_validation_issue_id=row.data_validation_issue_id,
                source_record_key=row.source_record_key,
                field_name=row.field_name,
                error_code=row.error_code,
                error_message=row.error_message,
                severity=row.severity,
                detected_at=row.detected_at,
            )
            for row in rows
        ]

    async def find_affected_delay_events(
        self, company_id: uuid.UUID, sku_id: uuid.UUID
    ) -> list[AffectedDelayEventRow]:
        """이 상품이 포함되고 현재 실행이 ON_HOLD 인 열린 지연(detected_at 최신순, 같으면 지연 ID)."""
        rows = await self.db.execute(
            _AFFECTED_DELAY_EVENTS, {"company_id": company_id, "sku_id": sku_id}
        )
        return [
            AffectedDelayEventRow(
                delay_event_id=row.delay_event_id,
                shipment_id=row.shipment_id,
                shipment_code=row.shipment_code,
                analysis_run_id=row.analysis_run_id,
                run_code=row.run_code,
                status=row.status,
                result_class=row.result_class,
                failure_code=row.failure_code,
            )
            for row in rows
        ]
