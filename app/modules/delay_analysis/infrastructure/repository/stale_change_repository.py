import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import UUID, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

"""
    STALE 판정용 변경 기록 조회 (D-13 의존 입력 기반 STALE, 정책 결정 ST-3)
      - 실행의 입력 순번(input_seq) 뒤에 적용된 같은 고객사 수집의 변경 기록을 적용 순번 순으로 읽습니다.
      - 1차 판정(관련 변경 거르기)에 필요한 값을 함께 붙여 반환합니다.
        재고 행 날짜, 변경된 선적과 그 입고 창고, 수집 전날 끝·수집 완료 시각의 예상 3PL 입고일
      - 판정 규칙은 데이터팀 엔진(tools/glowship_engine/stale.py first_filter)과 같은 기준을 씁니다.
      - 판정 대상 실행 후보와 진행 중 실행의 지연 선적 SKU(K_SHIPMENT)도 여기서 읽습니다.
"""

_CHANGES_AFTER_SEQ = text(
    """
    SELECT r.applied_seq, r.started_at, r.finished_at,
           c.entity_type, c.change_type, c.entity_ref, c.sku_id, c.warehouse_id,
           c.changed_fields, c.change_summary,
           inv.inventory_date,
           sh.shipment_id, sh.warehouse_id AS shipment_warehouse_id,
           prev.expected_3pl_inbound_date AS inbound_date_before,
           cur.expected_3pl_inbound_date AS inbound_date_after
      FROM data_change_records c
      JOIN data_sync_runs r
        ON r.company_id = c.company_id AND r.data_sync_run_id = c.data_sync_run_id
      LEFT JOIN inventory_daily inv
        ON c.entity_type = 'INVENTORY'
       AND inv.company_id = c.company_id AND inv.inventory_daily_id = c.entity_ref
      LEFT JOIN shipment_lines sl
        ON c.entity_type = 'SHIPMENT_LINE'
       AND sl.company_id = c.company_id AND sl.shipment_line_id = c.entity_ref
      LEFT JOIN shipments sh
        ON sh.company_id = c.company_id
       AND sh.shipment_id = CASE c.entity_type
                                WHEN 'SHIPMENT' THEN c.entity_ref
                                WHEN 'SHIPMENT_LINE' THEN sl.shipment_id
                            END
      LEFT JOIN LATERAL (
            SELECT s.expected_3pl_inbound_date
              FROM shipment_schedule_snapshots s
             WHERE s.company_id = c.company_id AND s.shipment_id = sh.shipment_id
               AND s.observed_at <= date_trunc('day', r.started_at, 'UTC') - interval '1 second'
             ORDER BY s.observed_at DESC
             LIMIT 1) prev ON true
      LEFT JOIN LATERAL (
            SELECT s.expected_3pl_inbound_date
              FROM shipment_schedule_snapshots s
             WHERE s.company_id = c.company_id AND s.shipment_id = sh.shipment_id
               AND s.observed_at <= r.finished_at
             ORDER BY s.observed_at DESC
             LIMIT 1) cur ON true
     WHERE c.company_id = :company_id
       AND r.applied_seq > :after_seq
       AND (CAST(:up_to_seq AS integer) IS NULL OR r.applied_seq <= :up_to_seq)
     ORDER BY r.applied_seq, c.data_change_record_id
    """
).bindparams(bindparam("company_id", type_=UUID(as_uuid=True)))


# STALE 판정 대상: 현재 실행 버전이고 진행 중·READY_FOR_REVIEW·NO_RISK 이며 지연 에피소드가 열려 있는 실행
# (FINALIZED·FAILED·ON_HOLD·NO_DELAY·STALE, 종료된 에피소드의 실행은 동결)
_TARGET_RUN_IDS = text(
    """
    SELECT a.analysis_run_id
      FROM analysis_runs a
      JOIN delay_events e
        ON e.company_id = a.company_id AND e.delay_event_id = a.delay_event_id
     WHERE a.company_id = :company_id
       AND a.is_latest
       AND a.status IN ('DETECTED','VALIDATING','ANALYZING_IMPACT','ANALYZING_OPTIONS',
                        'VALIDATING_OUTPUT','READY_FOR_REVIEW','NO_RISK')
       AND a.input_seq < :applied_seq
       AND e.closed_at IS NULL
     ORDER BY a.analysis_run_id
    """
).bindparams(bindparam("company_id", type_=UUID(as_uuid=True)))

# 진행 중 실행의 보수 판정용: 지연 선적과 그 선적 상세의 SKU 전체(K_SHIPMENT)
_DELAYED_SHIPMENT_SKUS = text(
    """
    SELECT e.shipment_id, sl.sku_id
      FROM delay_events e
      LEFT JOIN shipment_lines sl
        ON sl.company_id = e.company_id AND sl.shipment_id = e.shipment_id
     WHERE e.company_id = :company_id AND e.delay_event_id = :delay_event_id
    """
).bindparams(
    bindparam("company_id", type_=UUID(as_uuid=True)),
    bindparam("delay_event_id", type_=UUID(as_uuid=True)),
)


@dataclass(frozen=True)
class ScopedChange:
    """변경 기록 1건과 1차 판정에 쓰는 주변 값"""

    applied_seq: int
    sync_started_at: datetime
    sync_finished_at: datetime | None
    entity_type: str
    change_type: str
    entity_ref: uuid.UUID
    sku_id: uuid.UUID | None
    warehouse_id: uuid.UUID | None
    changed_fields: tuple[str, ...]
    change_summary: str
    inventory_date: date | None = None
    shipment_id: uuid.UUID | None = None
    shipment_warehouse_id: uuid.UUID | None = None
    inbound_date_before: date | None = None
    inbound_date_after: date | None = None

    @property
    def sync_date(self) -> date:
        """수집일 (수집 시작 시각의 UTC 날짜)"""
        return self.sync_started_at.astimezone(UTC).date()


class StaleChangeRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # Read
    async def find_target_run_ids(
        self, company_id: uuid.UUID, applied_seq: int
    ) -> list[uuid.UUID]:
        """적용 순번 applied_seq 의 수집으로 STALE 판정을 받아야 하는 실행 ID (잠그기 전 후보)"""
        result = await self.db.scalars(
            _TARGET_RUN_IDS, {"company_id": company_id, "applied_seq": applied_seq}
        )
        return list(result)

    async def find_delayed_shipment_skus(
        self, company_id: uuid.UUID, delay_event_id: uuid.UUID
    ) -> tuple[uuid.UUID, list[uuid.UUID]] | None:
        """지연 이벤트의 선적 ID와 그 선적 상세 SKU 목록. 지연 이벤트가 없으면 None"""
        rows = (
            await self.db.execute(
                _DELAYED_SHIPMENT_SKUS,
                {"company_id": company_id, "delay_event_id": delay_event_id},
            )
        ).all()
        if not rows:
            return None
        return rows[0].shipment_id, sorted({r.sku_id for r in rows if r.sku_id})

    async def find_changes_after(
        self,
        company_id: uuid.UUID,
        after_seq: int,
        up_to_seq: int | None = None,
    ) -> list[ScopedChange]:
        """after_seq 초과(·up_to_seq 이하) 적용 순번의 변경 기록을 적용 순번 순으로 반환합니다."""
        result = await self.db.execute(
            _CHANGES_AFTER_SEQ,
            {"company_id": company_id, "after_seq": after_seq, "up_to_seq": up_to_seq},
        )
        return [
            ScopedChange(
                applied_seq=row.applied_seq,
                sync_started_at=row.started_at,
                sync_finished_at=row.finished_at,
                entity_type=row.entity_type,
                change_type=row.change_type,
                entity_ref=row.entity_ref,
                sku_id=row.sku_id,
                warehouse_id=row.warehouse_id,
                changed_fields=tuple(row.changed_fields or ()),
                change_summary=row.change_summary,
                inventory_date=row.inventory_date,
                shipment_id=row.shipment_id,
                shipment_warehouse_id=row.shipment_warehouse_id,
                inbound_date_before=row.inbound_date_before,
                inbound_date_after=row.inbound_date_after,
            )
            for row in result
        ]
