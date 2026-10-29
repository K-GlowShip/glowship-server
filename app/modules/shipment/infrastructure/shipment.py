import uuid
from datetime import date

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.orm import Mapped

from app.core.orm import Base, uuid_pk


class Shipment(Base):
    """한국 → 미국 3PL 입고 선적 (현재 단계·예상 입고일 포함)"""

    __tablename__ = "shipments"
    __table_args__ = (
        UniqueConstraint("company_id", "shipment_id", name="shipments_uk1"),
        UniqueConstraint("company_id", "shipment_code", name="shipments_uk2"),
        CheckConstraint("transport_mode IN ('OCEAN','AIR')", name="shipments_ck1"),
        CheckConstraint(
            "current_stage IN ('PRE_DEPARTURE','AT_SEA','IN_AIR','AT_PORT','RECEIVED')",
            name="shipments_ck2",
        ),
        CheckConstraint(
            "(current_stage = 'RECEIVED') = (actual_3pl_inbound_date IS NOT NULL)",
            name="shipments_ck3",
        ),
        ForeignKeyConstraint(
            ["company_id", "warehouse_id"],
            ["warehouses.company_id", "warehouses.warehouse_id"],
            name="fk_shipments__warehouse_id",
        ),
        ForeignKeyConstraint(
            ["market_leadtime_reference_id"],
            ["market_leadtime_references.market_leadtime_reference_id"],
            name="fk_shipments__market_leadtime_reference_id",
        ),
        ForeignKeyConstraint(
            ["company_id", "data_sync_run_id"],
            ["data_sync_runs.company_id", "data_sync_runs.data_sync_run_id"],
            name="fk_shipments__data_sync_run_id",
        ),
    )

    shipment_id: Mapped[uuid_pk]
    company_id: Mapped[uuid.UUID]
    shipment_code: Mapped[str]
    transport_mode: Mapped[str]
    warehouse_id: Mapped[uuid.UUID]
    market_leadtime_reference_id: Mapped[uuid.UUID]
    planned_kr_release_date: Mapped[date]
    planned_3pl_inbound_date: Mapped[date]
    current_stage: Mapped[str]
    current_expected_port_arrival_date: Mapped[date | None]
    current_expected_3pl_inbound_date: Mapped[date]
    actual_3pl_inbound_date: Mapped[date | None]
    data_sync_run_id: Mapped[uuid.UUID]
