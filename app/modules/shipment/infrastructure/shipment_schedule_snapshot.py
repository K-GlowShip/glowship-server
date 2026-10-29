import uuid
from datetime import date, datetime

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.orm import Mapped

from app.core.orm import Base, uuid_pk


class ShipmentScheduleSnapshot(Base):
    """수집 실행 시점별 선적 일정 스냅샷 (지연 감지 비교 기준)"""

    __tablename__ = "shipment_schedule_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "shipment_schedule_snapshot_id",
            name="shipment_schedule_snapshots_uk1",
        ),
        UniqueConstraint(
            "shipment_id", "data_sync_run_id", name="shipment_schedule_snapshots_uk2"
        ),
        CheckConstraint(
            "stage IN ('PRE_DEPARTURE','AT_SEA','IN_AIR','AT_PORT','RECEIVED')",
            name="shipment_schedule_snapshots_ck1",
        ),
        ForeignKeyConstraint(
            ["company_id", "shipment_id"],
            ["shipments.company_id", "shipments.shipment_id"],
            name="fk_shipment_schedule_snapshots__shipment_id",
        ),
        ForeignKeyConstraint(
            ["company_id", "data_sync_run_id"],
            ["data_sync_runs.company_id", "data_sync_runs.data_sync_run_id"],
            name="fk_shipment_schedule_snapshots__data_sync_run_id",
        ),
    )

    shipment_schedule_snapshot_id: Mapped[uuid_pk]
    company_id: Mapped[uuid.UUID]
    shipment_id: Mapped[uuid.UUID]
    data_sync_run_id: Mapped[uuid.UUID]
    observed_at: Mapped[datetime]
    stage: Mapped[str]
    expected_port_arrival_date: Mapped[date | None]
    expected_3pl_inbound_date: Mapped[date]
