import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import ARRAY, UUID, bindparam, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.delay_analysis.infrastructure.delay_event import DelayEvent

"""
    지연 에피소드 저장 (할 일 9 지연 감지, GLOW-16, docs/analysis/delay-detection.md 4절)
      - 수집 적재가 선적 일정 스냅샷을 남긴 같은 트랜잭션 안에서, 그 수집의 스냅샷과 판정에 필요한 값을 읽고
        에피소드 행을 추가·종료한다. 판정은 하지 않는다(episode_rules.decide_episode 몫).
      - 데이터 접근 방식(docs/backend/data-access.md): 읽기는 LATERAL·FILTER 를 쓰는 직접 SQL,
        에피소드 한 행 추가는 ORM 엔터티(db.add + flush), 종료는 열린 행만 고치는 조건부 update(DelayEvent) 식.
      - 종료는 closed_at·close_reason 두 열만 고친다. 운영의 워커 역할에는 이 두 열의 UPDATE 권한이 필요하다
        (ERD 설명 4절 결정 2026-11-04, 서버 리비전은 개발자 3). 로컬 개발 DB 는 슈퍼유저라 지금도 동작한다.
      - 운영에서는 RLS 가 고객사 범위를 강제하지만, 로컬 개발 DB 는 슈퍼유저라 RLS 를 우회하므로
        조건을 명시해 같은 결과가 나오게 한다 (ERD 설명 4.6).
"""

_UUID = UUID(as_uuid=True)

_PRIOR_APPLIED_SHIPMENT_SYNC = text(
    """
    SELECT EXISTS (
        SELECT 1
          FROM data_sync_runs
         WHERE company_id = :company_id
           AND source_domain = 'SHIPMENT'
           AND status IN ('SUCCEEDED', 'PARTIAL_SUCCESS')
           AND loaded_record_count > 0
           AND data_sync_run_id <> :data_sync_run_id
    )
    """
).bindparams(
    bindparam("company_id", type_=_UUID), bindparam("data_sync_run_id", type_=_UUID)
)

# 한 수집은 선적마다 스냅샷을 많아야 하나 남긴다 (ShipmentDiff.schedule_changed).
# 직전 스냅샷 = 같은 선적의 다른 스냅샷 중 관측 시각이 가장 늦은 것 (관측 시각이 이번보다 늦지 않은 것).
_RUN_SNAPSHOTS = text(
    """
    SELECT s.shipment_schedule_snapshot_id, s.shipment_id, s.observed_at, s.stage,
           s.expected_3pl_inbound_date, sh.planned_3pl_inbound_date,
           prev.shipment_schedule_snapshot_id AS previous_snapshot_id,
           prev.stage AS previous_stage,
           prev.expected_3pl_inbound_date AS previous_expected_3pl_inbound_date
      FROM shipment_schedule_snapshots s
      JOIN shipments sh ON sh.company_id = s.company_id AND sh.shipment_id = s.shipment_id
      LEFT JOIN LATERAL (
        SELECT p.shipment_schedule_snapshot_id, p.stage, p.expected_3pl_inbound_date
          FROM shipment_schedule_snapshots p
         WHERE p.company_id = s.company_id AND p.shipment_id = s.shipment_id
           AND p.shipment_schedule_snapshot_id <> s.shipment_schedule_snapshot_id
           AND p.observed_at <= s.observed_at
         ORDER BY p.observed_at DESC, p.shipment_schedule_snapshot_id DESC
         LIMIT 1
      ) prev ON true
     WHERE s.company_id = :company_id AND s.data_sync_run_id = :data_sync_run_id
     ORDER BY s.shipment_id
    """
).bindparams(
    bindparam("company_id", type_=_UUID), bindparam("data_sync_run_id", type_=_UUID)
)

_EPISODES_OF_SHIPMENTS = text(
    """
    SELECT shipment_id,
           MAX(episode_no) AS last_episode_no,
           (ARRAY_AGG(delay_event_id) FILTER (WHERE closed_at IS NULL))[1] AS open_delay_event_id
      FROM delay_events
     WHERE company_id = :company_id AND shipment_id = ANY(:shipment_ids)
     GROUP BY shipment_id
    """
).bindparams(
    bindparam("company_id", type_=_UUID),
    bindparam("shipment_ids", type_=ARRAY(_UUID)),
)


@dataclass(frozen=True)
class RunSnapshot:
    """이번 수집이 남긴 일정 스냅샷 1건과 판정 재료 (선적의 계획 입고일, 직전 스냅샷)"""

    snapshot_id: uuid.UUID
    shipment_id: uuid.UUID
    observed_at: datetime
    stage: str
    expected_3pl_inbound_date: date
    planned_3pl_inbound_date: date
    previous_snapshot_id: uuid.UUID | None
    previous_stage: str | None
    previous_expected_3pl_inbound_date: date | None


@dataclass(frozen=True)
class ShipmentEpisodes:
    """선적의 에피소드 현황: 마지막 번호와 열린 에피소드(한 선적에 하나, delay_events_open_uq)"""

    last_episode_no: int
    open_delay_event_id: uuid.UUID | None


@dataclass(frozen=True)
class NewDelayEvent:
    delay_event_id: uuid.UUID
    shipment_id: uuid.UUID
    episode_no: int
    detected_snapshot_id: uuid.UUID
    previous_snapshot_id: uuid.UUID | None
    stage: str
    planned_3pl_inbound_date: date
    expected_3pl_inbound_date: date
    detected_at: datetime


def dedupe_key(shipment_id: uuid.UUID, episode_no: int) -> str:
    """delay_events_ck6: shipment_id::text || ':EP' || episode_no::text"""
    return f"{shipment_id}:EP{episode_no}"


class DelayEventWriteRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # Read
    async def has_prior_applied_shipment_sync(
        self, company_id: uuid.UUID, data_sync_run_id: uuid.UUID
    ) -> bool:
        """이 수집 말고 선적을 1건 이상 적재한(SUCCEEDED·PARTIAL_SUCCESS, loaded_record_count > 0) SHIPMENT 수집이 있는가.
        없으면 이번이 초기 적재(결정 1-10). 0건 수집은 기준 스냅샷을 남기지 않으므로 앞선 수집으로 치지 않는다."""
        return bool(
            await self.db.scalar(
                _PRIOR_APPLIED_SHIPMENT_SYNC,
                {"company_id": company_id, "data_sync_run_id": data_sync_run_id},
            )
        )

    async def find_run_snapshots(
        self, company_id: uuid.UUID, data_sync_run_id: uuid.UUID
    ) -> list[RunSnapshot]:
        """이 수집이 남긴 일정 스냅샷과 각 선적의 계획 입고일·직전 스냅샷 (선적 순)"""
        rows = await self.db.execute(
            _RUN_SNAPSHOTS,
            {"company_id": company_id, "data_sync_run_id": data_sync_run_id},
        )
        return [
            RunSnapshot(
                snapshot_id=row.shipment_schedule_snapshot_id,
                shipment_id=row.shipment_id,
                observed_at=row.observed_at,
                stage=row.stage,
                expected_3pl_inbound_date=row.expected_3pl_inbound_date,
                planned_3pl_inbound_date=row.planned_3pl_inbound_date,
                previous_snapshot_id=row.previous_snapshot_id,
                previous_stage=row.previous_stage,
                previous_expected_3pl_inbound_date=row.previous_expected_3pl_inbound_date,
            )
            for row in rows
        ]

    async def find_shipment_episodes(
        self, company_id: uuid.UUID, shipment_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, ShipmentEpisodes]:
        """선적별 마지막 에피소드 번호와 열린 에피소드. 에피소드가 없는 선적은 결과에 없다."""
        if not shipment_ids:
            return {}
        rows = await self.db.execute(
            _EPISODES_OF_SHIPMENTS,
            {"company_id": company_id, "shipment_ids": list(shipment_ids)},
        )
        return {
            row.shipment_id: ShipmentEpisodes(
                last_episode_no=int(row.last_episode_no),
                open_delay_event_id=row.open_delay_event_id,
            )
            for row in rows
        }

    # Write
    async def insert_delay_event(
        self, company_id: uuid.UUID, event: NewDelayEvent
    ) -> None:
        """에피소드 시작: delay_events 한 행 (dedupe_key = 선적:EP{번호}, ck6·uk2·uk3·open_uq)"""
        self.db.add(
            DelayEvent(
                delay_event_id=event.delay_event_id,
                company_id=company_id,
                shipment_id=event.shipment_id,
                episode_no=event.episode_no,
                detected_shipment_schedule_snapshot_id=event.detected_snapshot_id,
                previous_shipment_schedule_snapshot_id=event.previous_snapshot_id,
                stage=event.stage,
                planned_3pl_inbound_date=event.planned_3pl_inbound_date,
                expected_3pl_inbound_date=event.expected_3pl_inbound_date,
                dedupe_key=dedupe_key(event.shipment_id, event.episode_no),
                detected_at=event.detected_at,
            )
        )
        await self.db.flush()

    async def close_delay_event(
        self,
        company_id: uuid.UUID,
        delay_event_id: uuid.UUID,
        *,
        closed_at: datetime,
        close_reason: str,
    ) -> None:
        """에피소드 종료: 열린 행의 closed_at·close_reason 만 한 번 기록한다 (이미 닫혔으면 바꾸지 않음)."""
        await self.db.execute(
            update(DelayEvent)
            .where(
                DelayEvent.company_id == company_id,
                DelayEvent.delay_event_id == delay_event_id,
                DelayEvent.closed_at.is_(None),
            )
            .values(closed_at=closed_at, close_reason=close_reason)
        )
