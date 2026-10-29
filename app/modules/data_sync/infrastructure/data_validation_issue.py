import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.orm import Mapped

from app.core.orm import Base, uuid_pk


class DataValidationIssue(Base):
    """수집 데이터 검증 오류·경고"""

    __tablename__ = "data_validation_issues"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "data_validation_issue_id", name="data_validation_issues_uk1"
        ),
        CheckConstraint(
            "severity IN ('ERROR','WARNING')", name="data_validation_issues_ck1"
        ),
        CheckConstraint(
            "severity <> 'ERROR' OR blocks_analysis = true",
            name="data_validation_issues_ck2",
        ),
        CheckConstraint(
            "status IN ('OPEN','RESOLVED')", name="data_validation_issues_ck3"
        ),
        CheckConstraint(
            "(status = 'RESOLVED') = (resolved_at IS NOT NULL)",
            name="data_validation_issues_ck4",
        ),
        ForeignKeyConstraint(
            ["company_id", "data_sync_run_id"],
            ["data_sync_runs.company_id", "data_sync_runs.data_sync_run_id"],
            name="fk_data_validation_issues__data_sync_run_id",
        ),
    )

    data_validation_issue_id: Mapped[uuid_pk]
    company_id: Mapped[uuid.UUID]
    data_sync_run_id: Mapped[uuid.UUID]
    source_entity_type: Mapped[str]
    source_record_key: Mapped[str]
    field_name: Mapped[str | None]
    error_code: Mapped[str]
    error_message: Mapped[str]
    severity: Mapped[str]
    blocks_analysis: Mapped[bool]
    detected_at: Mapped[datetime]
    status: Mapped[str]
    resolved_at: Mapped[datetime | None]
