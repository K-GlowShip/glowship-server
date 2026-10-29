import uuid

from sqlalchemy import ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.orm import Mapped

from app.core.orm import Base, uuid_pk


class AnalysisRunDataVersion(Base):
    """분석 실행이 입력으로 사용한 수집 실행 목록"""

    __tablename__ = "analysis_run_data_versions"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "analysis_run_data_version_id",
            name="analysis_run_data_versions_uk1",
        ),
        UniqueConstraint(
            "analysis_run_id", "data_sync_run_id", name="analysis_run_data_versions_uk2"
        ),
        ForeignKeyConstraint(
            ["company_id", "analysis_run_id"],
            ["analysis_runs.company_id", "analysis_runs.analysis_run_id"],
            name="fk_analysis_run_data_versions__analysis_run_id",
        ),
        ForeignKeyConstraint(
            ["company_id", "data_sync_run_id"],
            ["data_sync_runs.company_id", "data_sync_runs.data_sync_run_id"],
            name="fk_analysis_run_data_versions__data_sync_run_id",
        ),
    )

    analysis_run_data_version_id: Mapped[uuid_pk]
    company_id: Mapped[uuid.UUID]
    analysis_run_id: Mapped[uuid.UUID]
    data_sync_run_id: Mapped[uuid.UUID]
