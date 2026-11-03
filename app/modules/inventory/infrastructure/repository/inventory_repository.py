import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import UUID, Date, Integer, Text, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

"""
    재고 목록 조회 (API 명세 v6.2 4.3 GET /inventory, 연동 데이터 조회 화면 SCR-005 의 재고 탭)
      - 한 일자(inventory_date)의 창고·SKU별 재고에 창고·SKU 요약을 붙여 한 페이지를 돌려준다 (목록 1회 + 건수 1회).
      - 일자는 서비스가 정한다 (요청값 또는 고객사의 최신 적재 일자 find_latest_date). 한 일자 안에서 창고·SKU 조합은 하나다(uk2).
      - 필터: 창고·SKU·검색어(SKU 코드·상품명 부분 일치, 대소문자 무시). 값이 없으면(NULL) 그 조건은 통과한다.
      - 정렬은 창고 코드 → SKU 코드 → sku_id (명세 4.3, 응답 규칙 6.2).
      - 수량 규칙은 DB 제약 그대로다: on_hand = reserved + available (ck5). 여기서 다시 계산하지 않는다 (BR-003).
      - 검증 오류로 거부된 레코드는 inventory_daily 에 없다. 오류는 GET /validation-issues 몫이다.
      - 운영에서는 RLS 가 고객사 범위를 강제하지만, 로컬 개발 DB 는 슈퍼유저라 RLS 를 우회하므로
        조건을 명시해 같은 결과가 나오게 한다 (ERD 설명 4.6).
"""

_LIKE_ESCAPE = "\\"
_UUID = UUID(as_uuid=True)

_LATEST_DATE = text(
    "SELECT max(inventory_date) FROM inventory_daily WHERE company_id = :company_id"
).bindparams(bindparam("company_id", type_=_UUID))

_FILTERED = """
    WITH filtered AS (
        SELECT i.inventory_date,
               w.warehouse_id, w.warehouse_code, w.region_code,
               k.sku_id, k.sku_code, k.product_name,
               i.on_hand_quantity, i.reserved_quantity, i.available_quantity, i.outbound_quantity,
               i.data_sync_run_id
          FROM inventory_daily i
          JOIN warehouses w ON w.company_id = i.company_id AND w.warehouse_id = i.warehouse_id
          JOIN skus k ON k.company_id = i.company_id AND k.sku_id = i.sku_id
         WHERE i.company_id = :company_id
           AND i.inventory_date = :inventory_date
           AND (CAST(:warehouse_id AS uuid) IS NULL OR i.warehouse_id = :warehouse_id)
           AND (CAST(:sku_id AS uuid) IS NULL OR i.sku_id = :sku_id)
           AND (CAST(:pattern AS text) IS NULL
                OR k.sku_code ILIKE :pattern ESCAPE '\\'
                OR k.product_name ILIKE :pattern ESCAPE '\\')
    )
"""

_FILTER_PARAMS = (
    bindparam("company_id", type_=_UUID),
    bindparam("inventory_date", type_=Date()),
    bindparam("warehouse_id", type_=_UUID),
    bindparam("sku_id", type_=_UUID),
    bindparam("pattern", type_=Text()),
)

_LIST_PAGE = text(
    _FILTERED
    + """
    SELECT inventory_date, warehouse_id, warehouse_code, region_code,
           sku_id, sku_code, product_name,
           on_hand_quantity, reserved_quantity, available_quantity, outbound_quantity,
           data_sync_run_id
      FROM filtered
     ORDER BY warehouse_code, sku_code, sku_id
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


@dataclass(frozen=True)
class InventoryFilter:
    """목록 필터. inventory_date 는 서비스가 정한 조회 일자(필수)다."""

    inventory_date: date
    warehouse_id: uuid.UUID | None = None
    sku_id: uuid.UUID | None = None
    query: str | None = None


@dataclass(frozen=True)
class InventoryRow:
    """한 일자의 창고·SKU 재고 한 건과 창고·SKU 요약"""

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
class InventoryPage:
    rows: list[InventoryRow]
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


def _filter_params(company_id: uuid.UUID, filters: InventoryFilter) -> dict:
    return {
        "company_id": company_id,
        "inventory_date": filters.inventory_date,
        "warehouse_id": filters.warehouse_id,
        "sku_id": filters.sku_id,
        "pattern": _like_pattern(filters.query),
    }


class InventoryRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # Read
    async def find_latest_date(self, company_id: uuid.UUID) -> date | None:
        """고객사의 가장 최근 적재 일자. 적재된 재고가 없으면 None."""
        return await self.db.scalar(_LATEST_DATE, {"company_id": company_id})

    async def find_page(
        self,
        company_id: uuid.UUID,
        filters: InventoryFilter,
        *,
        offset: int,
        limit: int,
    ) -> InventoryPage:
        """한 일자의 재고 한 페이지(창고 코드·SKU 코드·sku_id 순)와 같은 필터를 적용한 전체 건수."""
        params = _filter_params(company_id, filters)
        rows = await self.db.execute(
            _LIST_PAGE, {**params, "offset": offset, "limit": limit}
        )
        total_count = await self.db.scalar(_COUNT_PAGE, params)
        return InventoryPage(
            rows=[InventoryRow(**row._mapping) for row in rows],
            total_count=total_count or 0,
        )
