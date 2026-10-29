import uuid

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.orm import Mapped

from app.core.orm import Base, uuid_pk


class DataChangeRecord(Base):
    """수집 실행별 엔티티 변경 기록 (entity_ref는 다형성 참조라 FK 없음)"""

    __tablename__ = "data_change_records"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "data_change_record_id", name="data_change_records_uk1"
        ),
        UniqueConstraint(
            "data_sync_run_id",
            "entity_type",
            "entity_ref",
            "sku_id",
            name="data_change_records_uk2",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(
            "entity_type IN"
            " ('ORDER','ORDER_LINE','SHIPMENT','SHIPMENT_LINE','INVENTORY','SKU','WAREHOUSE')",
            name="data_change_records_ck1",
        ),
        CheckConstraint(
            "change_type IN ('INSERT','UPDATE','REMOVE')",
            name="data_change_records_ck2",
        ),
        CheckConstraint(
            "(entity_type = 'WAREHOUSE') = (sku_id IS NULL)",
            name="data_change_records_ck3",
        ),
        CheckConstraint(
            "(entity_type = 'SKU') = (warehouse_id IS NULL)",
            name="data_change_records_ck4",
        ),
        ForeignKeyConstraint(
            ["company_id", "data_sync_run_id"],
            ["data_sync_runs.company_id", "data_sync_runs.data_sync_run_id"],
            name="fk_data_change_records__data_sync_run_id",
        ),
        ForeignKeyConstraint(
            ["company_id", "sku_id"],
            ["skus.company_id", "skus.sku_id"],
            name="fk_data_change_records__sku_id",
        ),
        ForeignKeyConstraint(
            ["company_id", "warehouse_id"],
            ["warehouses.company_id", "warehouses.warehouse_id"],
            name="fk_data_change_records__warehouse_id",
        ),
    )

    data_change_record_id: Mapped[uuid_pk]
    company_id: Mapped[uuid.UUID]
    data_sync_run_id: Mapped[uuid.UUID]
    entity_type: Mapped[str]
    entity_ref: Mapped[uuid.UUID]
    change_type: Mapped[str]
    sku_id: Mapped[uuid.UUID | None]
    warehouse_id: Mapped[uuid.UUID | None]
    change_summary: Mapped[str]
