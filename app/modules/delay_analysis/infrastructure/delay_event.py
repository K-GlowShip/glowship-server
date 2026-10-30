import uuid
from datetime import date, datetime

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.orm import Mapped

from app.core.orm import Base, uuid_pk


class DelayEvent(Base):
    """선적 지연 감지 이벤트 (예상 3PL 입고일이 계획보다 늦어진 경우)"""

    __tablename__ = "delay_events"
    __table_args__ = (
        UniqueConstraint("company_id", "delay_event_id", name="delay_events_uk1"),
        UniqueConstraint("company_id", "dedupe_key", name="delay_events_uk2"),
        CheckConstraint(
            "expected_3pl_inbound_date > planned_3pl_inbound_date",
            name="delay_events_ck1",
        ),
        CheckConstraint(
            "stage IN ('PRE_DEPARTURE','AT_SEA','IN_AIR','AT_PORT','RECEIVED')",
            name="delay_events_ck2",
        ),
        # v6.2: 에피소드 종료. 종료된 지연의 실행은 STALE·재분석·최종 선택 대상이 아닙니다 (동결)
        CheckConstraint(
            "(closed_at IS NULL) = (close_reason IS NULL)", name="delay_events_ck4"
        ),
        CheckConstraint(
            "close_reason IS NULL OR close_reason IN ('RECOVERED','RECEIVED')",
            name="delay_events_ck5",
        ),
        CheckConstraint(
            "closed_at IS NULL OR closed_at >= detected_at", name="delay_events_ck7"
        ),
        ForeignKeyConstraint(
            ["company_id", "shipment_id"],
            ["shipments.company_id", "shipments.shipment_id"],
            name="fk_delay_events__shipment_id",
        ),
        ForeignKeyConstraint(
            ["company_id", "detected_shipment_schedule_snapshot_id"],
            [
                "shipment_schedule_snapshots.company_id",
                "shipment_schedule_snapshots.shipment_schedule_snapshot_id",
            ],
            name="fk_delay_events__detected_shipment_schedule_snapshot_id",
        ),
        ForeignKeyConstraint(
            ["company_id", "previous_shipment_schedule_snapshot_id"],
            [
                "shipment_schedule_snapshots.company_id",
                "shipment_schedule_snapshots.shipment_schedule_snapshot_id",
            ],
            name="fk_delay_events__previous_shipment_schedule_snapshot_id",
        ),
    )

    delay_event_id: Mapped[uuid_pk]
    company_id: Mapped[uuid.UUID]
    shipment_id: Mapped[uuid.UUID]
    detected_shipment_schedule_snapshot_id: Mapped[uuid.UUID]
    previous_shipment_schedule_snapshot_id: Mapped[uuid.UUID | None]
    stage: Mapped[str]
    planned_3pl_inbound_date: Mapped[date]
    expected_3pl_inbound_date: Mapped[date]
    dedupe_key: Mapped[str]
    detected_at: Mapped[datetime]
    closed_at: Mapped[datetime | None]
    close_reason: Mapped[str | None]
