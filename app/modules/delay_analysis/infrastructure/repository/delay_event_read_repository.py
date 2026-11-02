import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import ARRAY, UUID, Text, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

"""
    지연 목록·상세 조회 (API 명세 4.4절 GET /delay-events, GET /delay-events/{delayEventId})
      - 지연 이벤트에 선적·도착 항구·현재 실행(is_latest)·최종 선택 여부·위험 주문 수를 붙여 읽습니다.
      - 모든 조회는 고객사(company_id)로 거릅니다. 로컬 superuser 접속은 RLS 를 우회하므로 조건을 직접 겁니다.
      - 경로·대표 상품·지연 일수 같은 표시값은 여기서 만들지 않고 서비스가 delay_display 로 계산합니다.
      - episode_no(delay_events), result_class·completed_status·trigger_reason(analysis_runs)은 v6.2 DDL 에만 있고
        develop 마이그레이션(0004까지)에는 없어 읽지 않습니다. 개발자 3의 0005 병합 후 연결합니다 (응답에서는 null).
"""

# 진행 중 상태 (탭 "분석 중")
_IN_PROGRESS = (
    "'DETECTED','VALIDATING','ANALYZING_IMPACT','ANALYZING_OPTIONS','VALIDATING_OUTPUT'"
)

# 저장된 계산 결과가 없는 상태: 위험 주문 수가 null 입니다 (API 명세 4.4절).
# FAILED 는 설명 생성만 실패(EXPLANATION_GENERATION_FAILED)했으면 계산 결과가 있으므로 따로 봅니다.
_NO_STORED_RESULT = f"{_IN_PROGRESS},'ON_HOLD','NO_DELAY'"
_EXPLANATION_ONLY_FAILURE = "EXPLANATION_GENERATION_FAILED"

# 목록·건수·상세가 함께 쓰는 지연 이벤트 + 선적 + 현재 실행 묶음
#   단계·계획·예상 입고일은 선적의 현재 값입니다. delay_events 의 값은 감지 시점 값이라 갱신하지 않습니다 (4.4절).
#   위험 주문은 상세(order_line) 1줄이 1행이므로 서로 다른 주문 수로 셉니다 (D21).
_BASE = f"""
    SELECT e.delay_event_id, e.shipment_id, s.current_stage AS stage,
           s.planned_3pl_inbound_date,
           s.current_expected_3pl_inbound_date AS expected_3pl_inbound_date,
           e.detected_at, e.closed_at, e.close_reason,
           e.previous_shipment_schedule_snapshot_id,
           s.shipment_code, s.transport_mode, s.warehouse_id,
           wh.warehouse_code, wh.region_code,
           m.us_port_name,
           (SELECT w.region_code
              FROM warehouses w
             WHERE w.company_id = e.company_id AND w.warehouse_type = 'KR_ORIGIN'
             ORDER BY w.warehouse_code
             LIMIT 1) AS origin_region_code,
           r.analysis_run_id AS run_id, r.status AS run_status,
           EXISTS (SELECT 1 FROM final_decisions f
                    WHERE f.company_id = r.company_id
                      AND f.analysis_run_id = r.analysis_run_id) AS has_final_decision,
           CASE WHEN r.analysis_run_id IS NULL
                  OR r.status IN ({_NO_STORED_RESULT})
                  OR (r.status = 'FAILED'
                      AND r.failure_code IS DISTINCT FROM '{_EXPLANATION_ONLY_FAILURE}')
                THEN NULL
                ELSE (SELECT count(DISTINCT l.order_id)
                        FROM at_risk_orders o
                        JOIN order_lines l
                          ON l.company_id = o.company_id
                         AND l.order_line_id = o.order_line_id
                       WHERE o.company_id = r.company_id
                         AND o.analysis_run_id = r.analysis_run_id)
           END AS at_risk_order_count
      FROM delay_events e
      JOIN shipments s
        ON s.company_id = e.company_id AND s.shipment_id = e.shipment_id
      JOIN warehouses wh
        ON wh.company_id = s.company_id AND wh.warehouse_id = s.warehouse_id
      JOIN market_leadtime_references m
        ON m.market_leadtime_reference_id = s.market_leadtime_reference_id
      LEFT JOIN analysis_runs r
        ON r.company_id = e.company_id AND r.delay_event_id = e.delay_event_id
       AND r.is_latest
     WHERE e.company_id = :company_id
"""

# 목록 필터. 값이 없으면(NULL) 그 조건은 통과합니다. 탭(상태 필터)은 건수 집계에서는 빼야 하므로 따로 둡니다.
_FILTERS = """
       AND (CAST(:warehouse_id AS uuid) IS NULL OR s.warehouse_id = :warehouse_id)
       AND (CAST(:detected_from AS timestamptz) IS NULL OR e.detected_at >= :detected_from)
       AND (CAST(:detected_to AS timestamptz) IS NULL OR e.detected_at < :detected_to)
       AND (CAST(:q AS text) IS NULL
            OR s.shipment_code ILIKE '%' || :q || '%'
            OR EXISTS (SELECT 1
                         FROM shipment_lines l
                         JOIN skus k ON k.company_id = l.company_id AND k.sku_id = l.sku_id
                        WHERE l.company_id = e.company_id AND l.shipment_id = e.shipment_id
                          AND (k.sku_code ILIKE '%' || :q || '%'
                               OR k.product_name ILIKE '%' || :q || '%')))
"""

_STATUS_FILTER = """
       AND (CAST(:run_statuses AS text[]) IS NULL OR r.status = ANY(:run_statuses))
       AND (CAST(:has_final_decision AS boolean) IS NULL
            OR EXISTS (SELECT 1 FROM final_decisions f
                        WHERE f.company_id = r.company_id
                          AND f.analysis_run_id = r.analysis_run_id) = :has_final_decision)
"""

_UUID = UUID(as_uuid=True)

# 한 페이지. limit 이 NULL 이면 제한 없이 전부 (LIMIT NULL = LIMIT ALL)
_LIST = text(
    _BASE
    + _FILTERS
    + _STATUS_FILTER
    + " ORDER BY e.detected_at DESC, e.delay_event_id"
    + " LIMIT CAST(:limit AS integer) OFFSET :offset"
).bindparams(
    bindparam("company_id", type_=_UUID),
    bindparam("warehouse_id", type_=_UUID),
    bindparam("run_statuses", type_=ARRAY(Text())),
)

# 페이지네이션 전체 개수: 탭·최종 선택 여부까지 목록과 같은 필터를 모두 적용합니다 (탭별 건수와 다름)
_COUNT_FILTERED = text(
    f"SELECT count(*) FROM ({_BASE + _FILTERS + _STATUS_FILTER}) b"
).bindparams(
    bindparam("company_id", type_=_UUID),
    bindparam("warehouse_id", type_=_UUID),
    bindparam("run_statuses", type_=ARRAY(Text())),
)

# 탭별 건수: 탭 필터만 빼고 같은 조건으로 셉니다
_COUNTS = text(
    f"""
    SELECT count(*) AS all_count,
           count(*) FILTER (WHERE b.run_status = 'READY_FOR_REVIEW') AS ready_for_review,
           count(*) FILTER (WHERE b.run_status IN ({_IN_PROGRESS})) AS analyzing,
           count(*) FILTER (WHERE b.run_status = 'ON_HOLD') AS on_hold,
           count(*) FILTER (WHERE b.run_status IN ('STALE','FAILED')) AS needs_reanalysis
      FROM ({_BASE + _FILTERS}) b
    """
).bindparams(
    bindparam("company_id", type_=_UUID), bindparam("warehouse_id", type_=_UUID)
)

_ONE = text(_BASE + " AND e.delay_event_id = :delay_event_id").bindparams(
    bindparam("company_id", type_=_UUID), bindparam("delay_event_id", type_=_UUID)
)

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

_SNAPSHOT_EXPECTED_DATE = text(
    """
    SELECT expected_3pl_inbound_date
      FROM shipment_schedule_snapshots
     WHERE company_id = :company_id AND shipment_schedule_snapshot_id = :snapshot_id
    """
).bindparams(
    bindparam("company_id", type_=_UUID), bindparam("snapshot_id", type_=_UUID)
)

_RUNS = text(
    """
    SELECT analysis_run_id, version_no, status, is_latest, trigger_type,
           parent_analysis_run_id, stale_detected_at, stale_primary_reason,
           started_at, finished_at
      FROM analysis_runs
     WHERE company_id = :company_id AND delay_event_id = :delay_event_id
     ORDER BY version_no DESC
    """
).bindparams(
    bindparam("company_id", type_=_UUID), bindparam("delay_event_id", type_=_UUID)
)


@dataclass(frozen=True)
class DelayEventRow:
    delay_event_id: uuid.UUID
    shipment_id: uuid.UUID
    stage: str
    planned_3pl_inbound_date: date
    expected_3pl_inbound_date: date
    detected_at: datetime
    closed_at: datetime | None
    close_reason: str | None
    previous_shipment_schedule_snapshot_id: uuid.UUID | None
    shipment_code: str
    transport_mode: str
    warehouse_id: uuid.UUID
    warehouse_code: str
    region_code: str
    us_port_name: str
    origin_region_code: str | None
    run_id: uuid.UUID | None
    run_status: str | None
    has_final_decision: bool
    at_risk_order_count: int | None


@dataclass(frozen=True)
class DelayEventCounts:
    all_count: int
    ready_for_review: int
    analyzing: int
    on_hold: int
    needs_reanalysis: int


@dataclass(frozen=True)
class ShipmentLineRow:
    shipment_id: uuid.UUID
    line_no: int
    sku_id: uuid.UUID
    shipped_quantity: int
    sku_code: str
    product_name: str


@dataclass(frozen=True)
class RunHistoryRow:
    analysis_run_id: uuid.UUID
    version_no: int
    status: str
    is_latest: bool
    trigger_type: str
    parent_analysis_run_id: uuid.UUID | None
    stale_detected_at: datetime | None
    stale_primary_reason: str | None
    started_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True)
class DelayEventFilter:
    """목록 필터. None 이면 그 조건을 걸지 않습니다. detected_to 는 그 시각 미만(배타)입니다."""

    warehouse_id: uuid.UUID | None = None
    detected_from: datetime | None = None
    detected_to: datetime | None = None
    q: str | None = None
    run_statuses: Sequence[str] | None = None
    has_final_decision: bool | None = None


class DelayEventReadRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # Read
    async def find_delay_events(
        self,
        company_id: uuid.UUID,
        filters: DelayEventFilter,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[DelayEventRow]:
        """필터에 맞는 지연 이벤트를 감지 시각 최신순으로 반환합니다. limit 이 없으면 전부 반환합니다."""
        params = _status_filter_params(company_id, filters) | {
            "limit": limit,
            "offset": offset,
        }
        result = await self.db.execute(_LIST, params)
        return [DelayEventRow(**row._mapping) for row in result]

    async def count_delay_events(
        self, company_id: uuid.UUID, filters: DelayEventFilter
    ) -> int:
        """목록과 같은 필터(탭·최종 선택 여부 포함)를 모두 적용한 전체 개수. 페이지네이션 totalCount 에 씁니다."""
        return await self.db.scalar(
            _COUNT_FILTERED, _status_filter_params(company_id, filters)
        )

    async def count_by_tab(
        self, company_id: uuid.UUID, filters: DelayEventFilter
    ) -> DelayEventCounts:
        """탭(현재 실행 상태)별 건수. 탭·최종 선택 여부 필터는 무시하고 나머지 필터는 같게 셉니다."""
        row = (
            await self.db.execute(_COUNTS, _filter_params(company_id, filters))
        ).one()
        return DelayEventCounts(**row._mapping)

    async def find_delay_event(
        self, company_id: uuid.UUID, delay_event_id: uuid.UUID
    ) -> DelayEventRow | None:
        row = (
            await self.db.execute(
                _ONE, {"company_id": company_id, "delay_event_id": delay_event_id}
            )
        ).one_or_none()
        return DelayEventRow(**row._mapping) if row is not None else None

    async def find_shipment_lines(
        self, company_id: uuid.UUID, shipment_ids: Sequence[uuid.UUID]
    ) -> list[ShipmentLineRow]:
        """여러 선적의 상세 줄과 SKU 코드·상품명 (선적·줄 번호 순)"""
        if not shipment_ids:
            return []
        result = await self.db.execute(
            _LINES, {"company_id": company_id, "shipment_ids": list(shipment_ids)}
        )
        return [ShipmentLineRow(**row._mapping) for row in result]

    async def find_snapshot_expected_date(
        self, company_id: uuid.UUID, snapshot_id: uuid.UUID
    ) -> date | None:
        """일정 관측(스냅샷)의 예상 3PL 입고일. 지연 상세의 '이전 예상일'에 씁니다."""
        return await self.db.scalar(
            _SNAPSHOT_EXPECTED_DATE,
            {"company_id": company_id, "snapshot_id": snapshot_id},
        )

    async def find_runs(
        self, company_id: uuid.UUID, delay_event_id: uuid.UUID
    ) -> list[RunHistoryRow]:
        """지연 이벤트의 분석 실행 버전 이력 (최신 버전 먼저)"""
        result = await self.db.execute(
            _RUNS, {"company_id": company_id, "delay_event_id": delay_event_id}
        )
        return [RunHistoryRow(**row._mapping) for row in result]


def _status_filter_params(company_id: uuid.UUID, filters: DelayEventFilter) -> dict:
    """탭·최종 선택 여부까지 포함한 목록 필터 값"""
    return _filter_params(company_id, filters) | {
        "run_statuses": list(filters.run_statuses)
        if filters.run_statuses is not None
        else None,
        "has_final_decision": filters.has_final_decision,
    }


def _filter_params(company_id: uuid.UUID, filters: DelayEventFilter) -> dict:
    return {
        "company_id": company_id,
        "warehouse_id": filters.warehouse_id,
        "detected_from": filters.detected_from,
        "detected_to": filters.detected_to,
        "q": filters.q,
    }
