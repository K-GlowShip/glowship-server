import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import ARRAY, UUID, DateTime, Integer, Text, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

"""
    주문 목록 조회 (API 명세 v6.2 4.3 GET /orders, 연동 데이터 조회 화면 SCR-005 의 주문 탭)
      - 주문 한 건에 출고 창고 요약을 붙여 한 페이지를 돌려주고, 페이지에 나온 주문의 상세 줄은 별도 문장으로 읽는다
        (목록 1회 + 건수 1회 + 상세 1회, 페이지당 N+1 없음).
      - 필터: 상태·창고·채널·주문 기간(UTC 시각, [from, to) 로 to 미포함)·검색어. 값이 없으면(NULL) 그 조건은 통과한다.
      - 검색어는 주문 번호 또는 주문 상품의 SKU 코드·상품명 부분 일치(대소문자 무시)다. LIKE 특수문자는 글자 그대로 찾는다.
      - 정렬은 ordered_at 최신순, 같으면 order_id (명세 4.3, 응답 본문 규칙 6.2).
      - 검증 오류로 거부된 주문은 orders 에 없다. 오류는 GET /validation-issues 몫이라 여기서 다루지 않는다.
      - 실제 출고일(actual_dispatch_date)은 저장된 열이 아니라 data_change_records 로 역산한다 (명세 D8 제안, 4.3 저장 위치).
        규칙은 참조 엔진 glowship_engine/store.py 113~122행과 같다: 주문의 상품 줄(ORDER_LINE)에 UPDATE 가 처음
        기록된 수집(data_sync_runs.started_at)의 UTC 날짜 − 1일. UPDATE 가 없으면(출고 전) NULL 이다.
        data_change_records 에 entity_ref 색인이 없어 한 페이지의 주문에만 조인한다 (한 페이지 약 30ms, 로컬 실측).
      - 재고 확보일·출고 예정일·배송 예정일은 엔진의 배정 재생(snapshot.py)이 필요해 여기서 계산하지 않는다 (서비스가 null).
      - 운영에서는 RLS 가 고객사 범위를 강제하지만, 로컬 개발 DB 는 슈퍼유저라 RLS 를 우회하므로
        조건을 명시해 같은 결과가 나오게 한다 (ERD 설명 4.6).
"""

_LIKE_ESCAPE = "\\"
_UUID = UUID(as_uuid=True)

_FILTERED = """
    WITH filtered AS (
        SELECT o.order_id, o.order_code, o.sales_channel, o.customer_region,
               o.ordered_at, o.promised_delivery_at, o.status,
               w.warehouse_id, w.warehouse_code, w.region_code
          FROM orders o
          JOIN warehouses w ON w.company_id = o.company_id AND w.warehouse_id = o.warehouse_id
         WHERE o.company_id = :company_id
           AND (CAST(:status AS text) IS NULL OR o.status = :status)
           AND (CAST(:warehouse_id AS uuid) IS NULL OR o.warehouse_id = :warehouse_id)
           AND (CAST(:sales_channel AS text) IS NULL OR o.sales_channel = :sales_channel)
           AND (CAST(:ordered_from AS timestamptz) IS NULL OR o.ordered_at >= :ordered_from)
           AND (CAST(:ordered_to AS timestamptz) IS NULL OR o.ordered_at < :ordered_to)
           AND (CAST(:pattern AS text) IS NULL
                OR o.order_code ILIKE :pattern ESCAPE '\\'
                OR EXISTS (SELECT 1
                             FROM order_lines l
                             JOIN skus k ON k.company_id = l.company_id AND k.sku_id = l.sku_id
                            WHERE l.company_id = o.company_id AND l.order_id = o.order_id
                              AND (k.sku_code ILIKE :pattern ESCAPE '\\'
                                   OR k.product_name ILIKE :pattern ESCAPE '\\')))
    )
"""

_FILTER_PARAMS = (
    bindparam("company_id", type_=_UUID),
    bindparam("status", type_=Text()),
    bindparam("warehouse_id", type_=_UUID),
    bindparam("sales_channel", type_=Text()),
    bindparam("ordered_from", type_=DateTime(timezone=True)),
    bindparam("ordered_to", type_=DateTime(timezone=True)),
    bindparam("pattern", type_=Text()),
)

# 한 페이지를 먼저 자른 뒤 그 주문들에 대해서만 실제 출고일을 역산한다 (D8: 첫 ORDER_LINE UPDATE 수집일 − 1).
_LIST_PAGE = text(
    _FILTERED
    + """
    , page AS (
        SELECT order_id, order_code, sales_channel, customer_region,
               ordered_at, promised_delivery_at, status,
               warehouse_id, warehouse_code, region_code
          FROM filtered
         ORDER BY ordered_at DESC, order_id
        OFFSET :offset LIMIT :limit
    ), dispatch AS (
        SELECT l.order_id,
               MIN((r.started_at AT TIME ZONE 'UTC')::date) - 1 AS actual_dispatch_date
          FROM page p
          JOIN order_lines l ON l.company_id = :company_id AND l.order_id = p.order_id
          JOIN data_change_records d
            ON d.company_id = :company_id AND d.entity_type = 'ORDER_LINE'
           AND d.change_type = 'UPDATE' AND d.entity_ref = l.order_line_id
          JOIN data_sync_runs r ON r.data_sync_run_id = d.data_sync_run_id
         GROUP BY l.order_id
    )
    SELECT p.order_id, p.order_code, p.sales_channel, p.customer_region,
           p.ordered_at, p.promised_delivery_at, p.status,
           dp.actual_dispatch_date,
           p.warehouse_id, p.warehouse_code, p.region_code
      FROM page p
      LEFT JOIN dispatch dp ON dp.order_id = p.order_id
     ORDER BY p.ordered_at DESC, p.order_id
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
    SELECT l.order_id, l.line_no, l.sku_id, k.sku_code, k.product_name,
           l.ordered_quantity, l.shipped_quantity, l.line_status
      FROM order_lines l
      JOIN skus k ON k.company_id = l.company_id AND k.sku_id = l.sku_id
     WHERE l.company_id = :company_id AND l.order_id = ANY(:order_ids)
     ORDER BY l.order_id, l.line_no
    """
).bindparams(
    bindparam("company_id", type_=_UUID),
    bindparam("order_ids", type_=ARRAY(_UUID)),
)


@dataclass(frozen=True)
class OrderFilter:
    """목록 필터. 기간은 UTC 시각이고 ordered_to 는 포함하지 않는 상한이다."""

    status: str | None = None
    warehouse_id: uuid.UUID | None = None
    sales_channel: str | None = None
    ordered_from: datetime | None = None
    ordered_to: datetime | None = None
    query: str | None = None


@dataclass(frozen=True)
class OrderListRow:
    """주문 한 건과 출고 창고 요약, 역산한 실제 출고일"""

    order_id: uuid.UUID
    order_code: str
    sales_channel: str
    customer_region: str
    ordered_at: datetime
    promised_delivery_at: datetime
    status: str
    actual_dispatch_date: date | None
    warehouse_id: uuid.UUID
    warehouse_code: str
    region_code: str


@dataclass(frozen=True)
class OrderLineRow:
    """주문 상세 한 줄과 SKU 요약"""

    order_id: uuid.UUID
    line_no: int
    sku_id: uuid.UUID
    sku_code: str
    product_name: str
    ordered_quantity: int
    shipped_quantity: int
    line_status: str


@dataclass(frozen=True)
class OrderListPage:
    rows: list[OrderListRow]
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


def _filter_params(company_id: uuid.UUID, filters: OrderFilter) -> dict:
    return {
        "company_id": company_id,
        "status": filters.status,
        "warehouse_id": filters.warehouse_id,
        "sales_channel": filters.sales_channel,
        "ordered_from": filters.ordered_from,
        "ordered_to": filters.ordered_to,
        "pattern": _like_pattern(filters.query),
    }


class OrderRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # Read
    async def find_page(
        self,
        company_id: uuid.UUID,
        filters: OrderFilter,
        *,
        offset: int,
        limit: int,
    ) -> OrderListPage:
        """고객사의 주문 한 페이지(ordered_at 최신순·order_id 순)와 같은 필터를 적용한 전체 건수."""
        params = _filter_params(company_id, filters)
        rows = await self.db.execute(
            _LIST_PAGE, {**params, "offset": offset, "limit": limit}
        )
        total_count = await self.db.scalar(_COUNT_PAGE, params)
        return OrderListPage(
            rows=[OrderListRow(**row._mapping) for row in rows],
            total_count=total_count or 0,
        )

    async def find_lines(
        self, company_id: uuid.UUID, order_ids: Sequence[uuid.UUID]
    ) -> list[OrderLineRow]:
        """여러 주문의 상세 줄과 SKU 코드·상품명 (주문·줄 번호 순). 주문이 없으면 조회하지 않는다."""
        if not order_ids:
            return []
        result = await self.db.execute(
            _LINES, {"company_id": company_id, "order_ids": list(order_ids)}
        )
        return [OrderLineRow(**row._mapping) for row in result]
