import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.orm import Mapped

from app.core.orm import Base, uuid_pk


class DataSyncRun(Base):
    """외부 데이터 수집 실행 이력 (applied_seq: 고객사별 적용 순번)"""

    __tablename__ = "data_sync_runs"
    __table_args__ = (
        UniqueConstraint("company_id", "data_sync_run_id", name="data_sync_runs_uk1"),
        UniqueConstraint("company_id", "applied_seq", name="data_sync_runs_uk2"),
        CheckConstraint(
            "source_domain IN ('PRODUCT','WAREHOUSE','ORDER','SHIPMENT','INVENTORY')",
            name="data_sync_runs_ck1",
        ),
        CheckConstraint(
            "connection_method IN ('API','FILE')", name="data_sync_runs_ck2"
        ),
        CheckConstraint(
            "status IN ('RUNNING','SUCCEEDED','PARTIAL_SUCCESS','FAILED')",
            name="data_sync_runs_ck3",
        ),
        CheckConstraint(
            "status NOT IN ('SUCCEEDED','PARTIAL_SUCCESS')"
            " OR (received_record_count IS NOT NULL AND loaded_record_count IS NOT NULL"
            " AND rejected_record_count IS NOT NULL)",
            name="data_sync_runs_ck4",
        ),
        CheckConstraint(
            "status NOT IN ('SUCCEEDED','PARTIAL_SUCCESS')"
            " OR received_record_count = loaded_record_count + rejected_record_count",
            name="data_sync_runs_ck5",
        ),
        CheckConstraint(
            "status <> 'SUCCEEDED' OR rejected_record_count = 0",
            name="data_sync_runs_ck6",
        ),
        CheckConstraint(
            "status <> 'PARTIAL_SUCCESS' OR rejected_record_count > 0",
            name="data_sync_runs_ck7",
        ),
        CheckConstraint(
            "(status IN ('SUCCEEDED','PARTIAL_SUCCESS')) = (applied_seq IS NOT NULL)",
            name="data_sync_runs_ck8",
        ),
        ForeignKeyConstraint(
            ["company_id"],
            ["companies.company_id"],
            name="fk_data_sync_runs__company_id",
        ),
    )

    data_sync_run_id: Mapped[uuid_pk]
    company_id: Mapped[uuid.UUID]
    source_domain: Mapped[str]
    connection_method: Mapped[str]
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    status: Mapped[str]
    received_record_count: Mapped[int | None]
    loaded_record_count: Mapped[int | None]
    rejected_record_count: Mapped[int | None]
    applied_seq: Mapped[int | None]
