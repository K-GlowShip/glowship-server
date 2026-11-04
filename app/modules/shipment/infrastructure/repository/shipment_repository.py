import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy import ARRAY, UUID, Integer, Text, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

"""
    선적 목록 조회 (API 명세 v6.2 4.3 GET /shipments, 연동 데이터 조회 화면 SCR-005 의 선적 탭)
      - 선적 한 건에 도착 창고 요약과 계획 근거(시장 리드타임 기준의 항구 이름)를 붙여 한 페이지를 돌려주고,
        페이지에 나온 선적의 상세 줄은 별도 문장으로 읽는다 (목록 1회 + 건수 1회 + 상세 1회, 페이지당 N+1 없음).
      - 필터: 단계·운송 수단·도착 창고·검색어(선적 코드 부분 일치, 대소문자 무시). 값이 없으면(NULL) 그 조건은 통과한다.
        검색어는 명세 4.3 대로 선적 코드만 본다 (주문 탭과 달리 SKU 코드·상품명은 보지 않는다).
      - 정렬은 planned_3pl_inbound_date 최신순, 같으면 shipment_id (명세 4.3, 응답 본문 규칙 6.2).
      - 단계·예상 입고일은 선적의 현재 값(current_stage·current_expected_3pl_inbound_date)이다 (명세 4.3).
      - 경로(route) 출발지는 고객사의 한국 출고 창고(KR_ORIGIN) 지역이다. 지연 목록 SQL 과 같은 서브쿼리를 쓴다.
      - 상세 줄은 대표 상품·품목 수 계산(D13)에 쓴다. 선적당 1~30줄이라 페이지의 선적 ID 배열로 한 번에 읽는다.
      - 검증 오류로 거부된 선적은 shipments 에 없다. 오류는 GET /validation-issues 몫이라 여기서 다루지 않는다.
      - 운영에서는 RLS 가 고객사 범위를 강제하지만, 로컬 개발 DB 는 슈퍼유저라 RLS 를 우회하므로
        조건을 명시해 같은 결과가 나오게 한다 (ERD 설명 4.6).
"""

_LIKE_ESCAPE = "\\"
_UUID = UUID(as_uuid=True)

_FILTERED = """
    WITH filtered AS (
        SELECT s.shipment_id, s.shipment_code, s.transport_mode,
               s.current_stage AS stage,
               s.planned_kr_release_date, s.planned_3pl_inbound_date,
               s.current_expected_3pl_inbound_date AS expected_3pl_inbound_date,
               s.actual_3pl_inbound_date,
               w.warehouse_id, w.warehouse_code, w.region_code,
               m.us_port_name,
               (SELECT o.region_code
                  FROM warehouses o
                 WHERE o.company_id = s.company_id AND o.warehouse_type = 'KR_ORIGIN'
                 ORDER BY o.warehouse_code
                 LIMIT 1) AS origin_region_code
          FROM shipments s
          JOIN warehouses w ON w.company_id = s.company_id AND w.warehouse_id = s.warehouse_id
          JOIN market_leadtime_references m
            ON m.market_leadtime_reference_id = s.market_leadtime_reference_id
         WHERE s.company_id = :company_id
           AND (CAST(:stage AS text) IS NULL OR s.current_stage = :stage)
           AND (CAST(:transport_mode AS text) IS NULL OR s.transport_mode = :transport_mode)
           AND (CAST(:warehouse_id AS uuid) IS NULL OR s.warehouse_id = :warehouse_id)
           AND (CAST(:pattern AS text) IS NULL OR s.shipment_code ILIKE :pattern ESCAPE '\\')
    )
"""

_FILTER_PARAMS = (
    bindparam("company_id", type_=_UUID),
    bindparam("stage", type_=Text()),
    bindparam("transport_mode", type_=Text()),
    bindparam("warehouse_id", type_=_UUID),
    bindparam("pattern", type_=Text()),
)

_LIST_PAGE = text(
    _FILTERED
    + """
    SELECT shipment_id, shipment_code, transport_mode, stage,
           planned_kr_release_date, planned_3pl_inbound_date,
           expected_3pl_inbound_date, actual_3pl_inbound_date,
           warehouse_id, warehouse_code, region_code,
           us_port_name, origin_region_code
      FROM filtered
     ORDER BY planned_3pl_inbound_date DESC, shipment_id
    OFFSET :offset LIMIT :limit
    """
).bindparams(
    *_FILTER_PARAMS,
    bindparam("offset", type_=Integer()),
    bindparam("limit", type_=Integer()),
)

# 페이지가 범위를 벗어나도 totalCount 를 알 수 있도록 건수는 별도 문장으로 센다.
_COUNT_PAGE = text(
    _FILTERED + "SELECT COUNT(*) AS total_count FROM filtered"
).bindparams(*_FILTER_PARAMS)

_LINES = text(
    """
    SELECT l.shipment_id, l.line_no, l.sku_id, l.shipped_quantity,
           k.sku_code, k.product_name
      FROM shipment_lines l
      JOIN skus k ON k.company_id = l.company_id AND k.sku_id = l.sku_id
     WHERE l.company_id = :company_id AND l.shipment_id = ANY(:shipment_ids)
     ORDER BY l.shipment_id, l.line_no
    """
).bindparams(
    bindparam("company_id", type_=_UUID),
    bindparam("shipment_ids", type_=ARRAY(_UUID)),
)


@dataclass(frozen=True)
class ShipmentFilter:
    """목록 필터. 모두 선택 사항이고 None 이면 그 조건은 통과한다."""

    stage: str | None = None
    transport_mode: str | None = None
    warehouse_id: uuid.UUID | None = None
    query: str | None = None


@dataclass(frozen=True)
class ShipmentListRow:
    """선적 한 건과 도착 창고 요약, 경로 계산에 쓰는 항구 이름·출발 지역"""

    shipment_id: uuid.UUID
    shipment_code: str
    transport_mode: str
    stage: str
    planned_kr_release_date: date
    planned_3pl_inbound_date: date
    expected_3pl_inbound_date: date
    actual_3pl_inbound_date: date | None
    warehouse_id: uuid.UUID
    warehouse_code: str
    region_code: str
    us_port_name: str
    origin_region_code: str | None


@dataclass(frozen=True)
class ShipmentLineRow:
    """선적 상세 한 줄과 SKU 요약"""

    shipment_id: uuid.UUID
    line_no: int
    sku_id: uuid.UUID
    shipped_quantity: int
    sku_code: str
    product_name: str


@dataclass(frozen=True)
class ShipmentListPage:
    rows: list[ShipmentListRow]
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


def _filter_params(company_id: uuid.UUID, filters: ShipmentFilter) -> dict:
    return {
        "company_id": company_id,
        "stage": filters.stage,
        "transport_mode": filters.transport_mode,
        "warehouse_id": filters.warehouse_id,
        "pattern": _like_pattern(filters.query),
    }


class ShipmentRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # Read
    async def find_page(
        self,
        company_id: uuid.UUID,
        filters: ShipmentFilter,
        *,
        offset: int,
        limit: int,
    ) -> ShipmentListPage:
        """고객사의 선적 한 페이지(계획 입고일 최신순·shipment_id 순)와 같은 필터를 적용한 전체 건수."""
        params = _filter_params(company_id, filters)
        rows = await self.db.execute(
            _LIST_PAGE, {**params, "offset": offset, "limit": limit}
        )
        total_count = await self.db.scalar(_COUNT_PAGE, params)
        return ShipmentListPage(
            rows=[ShipmentListRow(**row._mapping) for row in rows],
            total_count=total_count or 0,
        )

    async def find_lines(
        self, company_id: uuid.UUID, shipment_ids: Sequence[uuid.UUID]
    ) -> list[ShipmentLineRow]:
        """여러 선적의 상세 줄과 SKU 코드·상품명 (선적·줄 번호 순). 선적이 없으면 조회하지 않는다."""
        if not shipment_ids:
            return []
        result = await self.db.execute(
            _LINES, {"company_id": company_id, "shipment_ids": list(shipment_ids)}
        )
        return [ShipmentLineRow(**row._mapping) for row in result]
