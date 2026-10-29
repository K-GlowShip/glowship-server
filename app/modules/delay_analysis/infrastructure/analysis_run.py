import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped

from app.core.orm import Base, uuid_pk


class AnalysisRun(Base):
    """
    지연 이벤트 영향 분석 실행 (버전별, 재분석 시 parent_analysis_run_id로 연결)
      - delay_event별 is_latest 행은 하나만 존재합니다 (부분 고유 인덱스).
      - FAILED·ON_HOLD 상태일 때만 실패 요약 5컬럼(failed_step ~ failure_message)을 채웁니다.
    """

    __tablename__ = "analysis_runs"
    __table_args__ = (
        UniqueConstraint("company_id", "analysis_run_id", name="analysis_runs_uk1"),
        UniqueConstraint("delay_event_id", "version_no", name="analysis_runs_uk2"),
        CheckConstraint(
            "trigger_type IN ('AUTO','REANALYSIS')", name="analysis_runs_ck1"
        ),
        CheckConstraint(
            "(trigger_type = 'REANALYSIS') = (requested_by_user_id IS NOT NULL)",
            name="analysis_runs_ck2",
        ),
        CheckConstraint(
            "(trigger_type = 'REANALYSIS') = (requested_at IS NOT NULL)",
            name="analysis_runs_ck3",
        ),
        CheckConstraint(
            "trigger_type = 'REANALYSIS' OR request_reason IS NULL",
            name="analysis_runs_ck4",
        ),
        CheckConstraint(
            "explanation_validation_status IS NULL"
            " OR explanation_validation_status IN ('PASSED','FAILED')",
            name="analysis_runs_ck5",
        ),
        CheckConstraint(
            "(explanation_validation_status IS NOT NULL"
            " AND explanation_validation_status = 'PASSED')"
            " = (final_explanation_json IS NOT NULL)",
            name="analysis_runs_ck6",
        ),
        CheckConstraint(
            "(final_explanation_json IS NOT NULL) = (explanation_generated_at IS NOT NULL)",
            name="analysis_runs_ck7",
        ),
        CheckConstraint(
            "(version_no = 1) = (parent_analysis_run_id IS NULL)",
            name="analysis_runs_ck8",
        ),
        CheckConstraint(
            "status IN ('DETECTED','VALIDATING','ANALYZING_IMPACT','ANALYZING_OPTIONS',"
            "'VALIDATING_OUTPUT','READY_FOR_REVIEW','NO_DELAY','NO_RISK','ON_HOLD',"
            "'FAILED','STALE','FINALIZED')",
            name="analysis_runs_ck9",
        ),
        CheckConstraint(
            "(status IN ('FAILED','ON_HOLD')) = (failed_step IS NOT NULL)",
            name="analysis_runs_ck10",
        ),
        CheckConstraint(
            "(failed_step IS NULL) = (failure_code IS NULL)"
            " AND (failed_step IS NULL) = (attempt_count IS NULL)"
            " AND (failed_step IS NULL) = (retry_exhausted IS NULL)"
            " AND (failed_step IS NULL) = (failure_message IS NULL)",
            name="analysis_runs_ck11",
        ),
        CheckConstraint(
            "failed_step IS NULL OR failed_step IN"
            " ('VALIDATING','ANALYZING_IMPACT','ANALYZING_OPTIONS','VALIDATING_OUTPUT')",
            name="analysis_runs_ck12",
        ),
        CheckConstraint(
            "attempt_count IS NULL OR attempt_count BETWEEN 1 AND 3",
            name="analysis_runs_ck13",
        ),
        CheckConstraint(
            "retry_exhausted IS NULL OR NOT retry_exhausted OR attempt_count = 3",
            name="analysis_runs_ck14",
        ),
        ForeignKeyConstraint(
            ["company_id", "delay_event_id"],
            ["delay_events.company_id", "delay_events.delay_event_id"],
            name="fk_analysis_runs__delay_event_id",
        ),
        ForeignKeyConstraint(
            ["company_id", "parent_analysis_run_id"],
            ["analysis_runs.company_id", "analysis_runs.analysis_run_id"],
            name="fk_analysis_runs__parent_analysis_run_id",
        ),
        ForeignKeyConstraint(
            ["company_id", "operating_rule_set_id"],
            [
                "operating_rule_sets.company_id",
                "operating_rule_sets.operating_rule_set_id",
            ],
            name="fk_analysis_runs__operating_rule_set_id",
        ),
        ForeignKeyConstraint(
            ["company_id", "requested_by_user_id"],
            ["users.company_id", "users.user_id"],
            name="fk_analysis_runs__requested_by_user_id",
        ),
        Index(
            "analysis_runs_is_latest_uq",
            "delay_event_id",
            unique=True,
            postgresql_where=text("is_latest"),
        ),
    )

    analysis_run_id: Mapped[uuid_pk]
    company_id: Mapped[uuid.UUID]
    delay_event_id: Mapped[uuid.UUID]
    version_no: Mapped[int]
    parent_analysis_run_id: Mapped[uuid.UUID | None]
    operating_rule_set_id: Mapped[uuid.UUID]
    input_seq: Mapped[int]
    trigger_type: Mapped[str]
    requested_by_user_id: Mapped[uuid.UUID | None]
    request_reason: Mapped[str | None]
    requested_at: Mapped[datetime | None]
    status: Mapped[str]
    status_reason: Mapped[str | None]
    failed_step: Mapped[str | None]
    failure_code: Mapped[str | None]
    attempt_count: Mapped[int | None]
    retry_exhausted: Mapped[bool | None]
    failure_message: Mapped[str | None]
    is_latest: Mapped[bool]
    model_name: Mapped[str | None]
    prompt_version: Mapped[str | None]
    final_explanation_json: Mapped[dict[str, Any] | None]
    explanation_validation_status: Mapped[str | None]
    explanation_generated_at: Mapped[datetime | None]
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
