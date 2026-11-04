import uuid
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import UUID, Date, DateTime, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

"""
    대시보드 집계 조회 (API 명세 v6.2 4.4 GET /dashboard/summary·/inbound-trend, 홈 대시보드 SCR-002)
      - 대시보드 값은 조회할 때 계산하고 저장하지 않는다.
      - 기준 시각 T 는 고객사의 마지막 수집 완료 시각이다 (명세 C22). 서버의 지금 시각이 아니다.
        적재된 수집(SUCCEEDED·PARTIAL_SUCCESS)의 finished_at 최댓값이고, GET /data-sync-runs/latest 의 lastSyncedAt 과 같은 기준이다.
      - 입고 추이는 날짜별 선적 수다: 계획 = planned_3pl_inbound_date, 실제 = actual_3pl_inbound_date.
        0건인 날은 여기서 돌려주지 않고 서비스가 채운다.
      - 요약 KPI 의 현재 값 (명세 4.4 표, C22):
          진행 중인 선적 = current_stage <> RECEIVED 인 선적 수
          지연 선적     = 열린 에피소드(closed_at 없음) 중 현재 실행(is_latest)이 NO_DELAY 가 아니고
                          (실행이 없어도 센다) 그 지연의 어느 실행에도 최종 선택이 없는 것의 서로 다른 선적 수
          정시 입고율    = (시작일, 끝일] 에 입고된 선적 중 actual ≤ planned 인 비율 (서비스가 나눗셈, 0건이면 null)
          위험 주문     = 열린 에피소드(closed_at 없음) 중 현재 실행이 READY_FOR_REVIEW 인 지연들의 at_risk_orders 를
                          주문으로 묶은 서로 다른 주문 수 (D21). 입고로 닫힌 에피소드는 새 실행 없이 READY_FOR_REVIEW 로
                          남을 수 있지만 동결이고 선택도 막히므로(serviceflow 4절) 세지 않는다 (C22)
          미출고 주문   = status 가 OPEN·PARTIALLY_SHIPPED 인 주문 수 (위험 주문 비율의 분모)
      - 비교값의 과거 상태 (C22). 과거 값은 시각이 남는 열로만 되살린다:
          진행 중인 선적(시각 t) = t 까지 관측된 마지막 일정 스냅샷(observed_at ≤ t)의 stage 가 RECEIVED 가 아닌 선적 수.
                                  t 까지 스냅샷이 없는 선적은 세지 않는다.
          지연 선적(시각 t)     = detected_at ≤ t 이고 (closed_at 없음 또는 > t) 인 지연 중, t 까지 끝난 최신 실행
                                  (finished_at ≤ t, version_no 최대)이 NO_DELAY 가 아니고(끝난 실행이 없어도 센다)
                                  t 까지 최종 선택(decided_at ≤ t)이 없는 것의 서로 다른 선적 수.
                                  실행의 status 는 지금 값이지만 NO_DELAY 는 끝난 실행의 최종 상태라 바뀌지 않는다.
      - 운영에서는 RLS 가 고객사 범위를 강제하지만, 로컬 개발 DB 는 슈퍼유저라 RLS 를 우회하므로
        조건을 명시해 같은 결과가 나오게 한다 (ERD 설명 4.6).
"""

_UUID = UUID(as_uuid=True)

_BASIS_AT = text(
    """
    SELECT max(finished_at)
      FROM data_sync_runs
     WHERE company_id = :company_id
       AND status IN ('SUCCEEDED', 'PARTIAL_SUCCESS')
    """
).bindparams(bindparam("company_id", type_=_UUID))

_INBOUND_COUNTS = text(
    """
    WITH scoped AS (
        SELECT planned_3pl_inbound_date, actual_3pl_inbound_date
          FROM shipments
         WHERE company_id = :company_id
           AND (CAST(:warehouse_id AS uuid) IS NULL OR warehouse_id = :warehouse_id)
    ),
    marks AS (
        SELECT planned_3pl_inbound_date AS day, 1 AS planned, 0 AS actual
          FROM scoped
         WHERE planned_3pl_inbound_date BETWEEN :start_date AND :end_date
        UNION ALL
        SELECT actual_3pl_inbound_date, 0, 1
          FROM scoped
         WHERE actual_3pl_inbound_date BETWEEN :start_date AND :end_date
    )
    SELECT day, SUM(planned) AS planned_count, SUM(actual) AS actual_count
      FROM marks
     GROUP BY day
     ORDER BY day
    """
).bindparams(
    bindparam("company_id", type_=_UUID),
    bindparam("warehouse_id", type_=_UUID),
    bindparam("start_date", type_=Date()),
    bindparam("end_date", type_=Date()),
)


_IN_TRANSIT_SHIPMENTS = text(
    """
    SELECT COUNT(*)
      FROM shipments
     WHERE company_id = :company_id AND current_stage <> 'RECEIVED'
    """
).bindparams(bindparam("company_id", type_=_UUID))

_DELAYED_SHIPMENTS = text(
    """
    SELECT COUNT(DISTINCT e.shipment_id)
      FROM delay_events e
      LEFT JOIN analysis_runs r
        ON r.company_id = e.company_id AND r.delay_event_id = e.delay_event_id AND r.is_latest
     WHERE e.company_id = :company_id
       AND e.closed_at IS NULL
       AND (r.status IS NULL OR r.status <> 'NO_DELAY')
       AND NOT EXISTS (
           SELECT 1
             FROM final_decisions f
             JOIN analysis_runs fr
               ON fr.company_id = f.company_id AND fr.analysis_run_id = f.analysis_run_id
            WHERE f.company_id = e.company_id AND fr.delay_event_id = e.delay_event_id
       )
    """
).bindparams(bindparam("company_id", type_=_UUID))

_IN_TRANSIT_SHIPMENTS_AT = text(
    """
    SELECT COUNT(*)
      FROM (
        SELECT DISTINCT ON (shipment_id) shipment_id, stage
          FROM shipment_schedule_snapshots
         WHERE company_id = :company_id AND observed_at <= :at
         ORDER BY shipment_id, observed_at DESC, shipment_schedule_snapshot_id DESC
      ) last_seen
     WHERE stage <> 'RECEIVED'
    """
).bindparams(
    bindparam("company_id", type_=_UUID), bindparam("at", type_=DateTime(timezone=True))
)

_DELAYED_SHIPMENTS_AT = text(
    """
    SELECT COUNT(DISTINCT e.shipment_id)
      FROM delay_events e
      LEFT JOIN LATERAL (
        SELECT r.status
          FROM analysis_runs r
         WHERE r.company_id = e.company_id AND r.delay_event_id = e.delay_event_id
           AND r.finished_at <= :at
         ORDER BY r.version_no DESC
         LIMIT 1
      ) finished ON true
     WHERE e.company_id = :company_id
       AND e.detected_at <= :at
       AND (e.closed_at IS NULL OR e.closed_at > :at)
       AND (finished.status IS NULL OR finished.status <> 'NO_DELAY')
       AND NOT EXISTS (
           SELECT 1
             FROM final_decisions f
             JOIN analysis_runs fr
               ON fr.company_id = f.company_id AND fr.analysis_run_id = f.analysis_run_id
            WHERE f.company_id = e.company_id AND fr.delay_event_id = e.delay_event_id
              AND f.decided_at <= :at
       )
    """
).bindparams(
    bindparam("company_id", type_=_UUID), bindparam("at", type_=DateTime(timezone=True))
)

_INBOUND_TIMELINESS = text(
    """
    SELECT COUNT(*) AS received_count,
           COUNT(*) FILTER (WHERE actual_3pl_inbound_date <= planned_3pl_inbound_date) AS on_time_count
      FROM shipments
     WHERE company_id = :company_id
       AND actual_3pl_inbound_date > :after_date
       AND actual_3pl_inbound_date <= :until_date
    """
).bindparams(
    bindparam("company_id", type_=_UUID),
    bindparam("after_date", type_=Date()),
    bindparam("until_date", type_=Date()),
)

_AT_RISK_ORDERS = text(
    """
    SELECT COUNT(DISTINCT ol.order_id)
      FROM at_risk_orders a
      JOIN analysis_runs r
        ON r.company_id = a.company_id AND r.analysis_run_id = a.analysis_run_id
      JOIN delay_events e
        ON e.company_id = r.company_id AND e.delay_event_id = r.delay_event_id
      JOIN order_lines ol
        ON ol.company_id = a.company_id AND ol.order_line_id = a.order_line_id
     WHERE a.company_id = :company_id
       AND r.is_latest
       AND r.status = 'READY_FOR_REVIEW'
       AND e.closed_at IS NULL
    """
).bindparams(bindparam("company_id", type_=_UUID))

_UNSHIPPED_ORDERS = text(
    """
    SELECT COUNT(*)
      FROM orders
     WHERE company_id = :company_id AND status IN ('OPEN', 'PARTIALLY_SHIPPED')
    """
).bindparams(bindparam("company_id", type_=_UUID))


@dataclass(frozen=True)
class InboundTimeliness:
    """기간 안에 입고된 선적 수와 그중 계획일 이내 입고 수"""

    received_count: int
    on_time_count: int


@dataclass(frozen=True)
class InboundCount:
    """하루의 입고 예정·실제 입고 선적 수. 0건인 날은 행이 없다."""

    day: date
    planned_count: int
    actual_count: int


class DashboardRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # Read
    async def find_basis_at(self, company_id: uuid.UUID) -> datetime | None:
        """기준 시각 T: 적재된 수집의 마지막 완료 시각. 적재된 수집이 없으면 None."""
        return await self.db.scalar(_BASIS_AT, {"company_id": company_id})

    async def find_inbound_counts(
        self,
        company_id: uuid.UUID,
        *,
        start_date: date,
        end_date: date,
        warehouse_id: uuid.UUID | None = None,
    ) -> list[InboundCount]:
        """[start_date, end_date] 안의 날짜별 입고 예정·실제 입고 선적 수 (날짜 순, 0건인 날 제외)."""
        rows = await self.db.execute(
            _INBOUND_COUNTS,
            {
                "company_id": company_id,
                "warehouse_id": warehouse_id,
                "start_date": start_date,
                "end_date": end_date,
            },
        )
        return [
            InboundCount(
                day=row.day,
                planned_count=int(row.planned_count),
                actual_count=int(row.actual_count),
            )
            for row in rows
        ]

    async def count_in_transit_shipments(self, company_id: uuid.UUID) -> int:
        """current_stage <> RECEIVED 인 선적 수"""
        return await self.db.scalar(_IN_TRANSIT_SHIPMENTS, {"company_id": company_id})

    async def count_delayed_shipments(self, company_id: uuid.UUID) -> int:
        """열린 에피소드 중 현재 실행이 NO_DELAY 가 아니고 최종 선택 전인 지연의 서로 다른 선적 수 (C22)"""
        return await self.db.scalar(_DELAYED_SHIPMENTS, {"company_id": company_id})

    async def count_in_transit_shipments_at(
        self, company_id: uuid.UUID, at: datetime
    ) -> int:
        """시각 at 에 진행 중이던 선적 수: at 까지 마지막으로 관측된 스냅샷 단계가 RECEIVED 가 아닌 선적 (C22)"""
        return await self.db.scalar(
            _IN_TRANSIT_SHIPMENTS_AT, {"company_id": company_id, "at": at}
        )

    async def count_delayed_shipments_at(
        self, company_id: uuid.UUID, at: datetime
    ) -> int:
        """시각 at 의 지연 선적 수: 그때 열려 있고, 그때까지 끝난 최신 실행이 NO_DELAY 가 아니고, 최종 선택 전 (C22)"""
        return await self.db.scalar(
            _DELAYED_SHIPMENTS_AT, {"company_id": company_id, "at": at}
        )

    async def find_inbound_timeliness(
        self, company_id: uuid.UUID, *, after_date: date, until_date: date
    ) -> InboundTimeliness:
        """(after_date, until_date] 에 실제 입고된 선적 수와 그중 계획 3PL 입고일 이내 입고 수"""
        row = (
            await self.db.execute(
                _INBOUND_TIMELINESS,
                {
                    "company_id": company_id,
                    "after_date": after_date,
                    "until_date": until_date,
                },
            )
        ).one()
        return InboundTimeliness(
            received_count=int(row.received_count),
            on_time_count=int(row.on_time_count),
        )

    async def count_at_risk_orders(self, company_id: uuid.UUID) -> int:
        """열린 에피소드 중 현재 실행이 READY_FOR_REVIEW 인 지연들의 위험 주문 수 (서로 다른 주문, D21·C22)"""
        return await self.db.scalar(_AT_RISK_ORDERS, {"company_id": company_id})

    async def count_unshipped_orders(self, company_id: uuid.UUID) -> int:
        """미출고 주문 수: status 가 OPEN·PARTIALLY_SHIPPED (C22)"""
        return await self.db.scalar(_UNSHIPPED_ORDERS, {"company_id": company_id})
