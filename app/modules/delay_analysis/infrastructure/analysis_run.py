import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.orm import Base, uuid_pk

# STALE 사유 코드 (stale_reason_codes 원소). 2개 이상이면 주 사유는 MULTIPLE_RELEVANT_CHANGES 입니다.
STALE_REASON_CODES = (
    "DELAY_SCHEDULE_CHANGED",
    "DELAY_STAGE_CHANGED",
    "RELEVANT_ORDER_CHANGED",
    "RELEVANT_INVENTORY_CHANGED",
    "RELEVANT_INBOUND_CHANGED",
    "OPERATING_RULE_CHANGED",
    "RELEVANT_SKU_ATTRIBUTE_CHANGED",
    "TIME_ELAPSED",
)
MULTIPLE_RELEVANT_CHANGES = "MULTIPLE_RELEVANT_CHANGES"
_STALE_REASON_SQL = ",".join(f"'{code}'" for code in STALE_REASON_CODES)


class AnalysisRun(Base):
    """
    지연 이벤트 영향 분석 실행 (버전별, 재분석 시 parent_analysis_run_id로 연결)
      - delay_event별 is_latest 행은 하나만 존재합니다 (부분 고유 인덱스).
      - FAILED·ON_HOLD 상태일 때만 실패 요약 5컬럼(failed_step ~ failure_message)을 채웁니다.
      - STALE 상태일 때만 STALE 사유 4컬럼(stale_*)을 채웁니다. TIME_ELAPSED 만 stale_detected_seq 가 비어 있습니다.
      - dependency_scope 는 분석이 실제로 읽은 입력 범위(dependency_scope.v1)로, STALE 1차 판정에 씁니다.
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
        # v6.2: STALE 사유 (D-13, 정책 결정 ST-3)
        CheckConstraint(
            "(status = 'STALE') = (stale_primary_reason IS NOT NULL)",
            name="analysis_runs_ck27",
        ),
        CheckConstraint(
            "stale_primary_reason IS NULL OR stale_primary_reason IN ("
            f"{_STALE_REASON_SQL},'MULTIPLE_RELEVANT_CHANGES')",
            name="analysis_runs_ck28",
        ),
        CheckConstraint(
            "(stale_primary_reason IS NULL) = (stale_reason_codes IS NULL)"
            " AND (stale_primary_reason IS NULL) = (stale_detected_at IS NULL)",
            name="analysis_runs_ck29",
        ),
        CheckConstraint(
            "stale_reason_codes IS NULL"
            f" OR stale_reason_codes <@ ARRAY[{_STALE_REASON_SQL}]::text[]",
            name="analysis_runs_ck30",
        ),
        CheckConstraint(
            "stale_reason_codes IS NULL"
            " OR (stale_primary_reason = 'MULTIPLE_RELEVANT_CHANGES')"
            " = (cardinality(stale_reason_codes) >= 2)",
            name="analysis_runs_ck31",
        ),
        CheckConstraint(
            "stale_primary_reason IS NULL"
            " OR stale_primary_reason = 'MULTIPLE_RELEVANT_CHANGES'"
            " OR stale_reason_codes = ARRAY[stale_primary_reason]",
            name="analysis_runs_ck32",
        ),
        CheckConstraint(
            "stale_primary_reason IS NULL"
            " OR (stale_primary_reason = 'TIME_ELAPSED') = (stale_detected_seq IS NULL)",
            name="analysis_runs_ck33",
        ),
        CheckConstraint(
            "status NOT IN ('ON_HOLD','FAILED') OR dependency_scope IS NULL",
            name="analysis_runs_ck36",
        ),
        CheckConstraint(
            "dependency_scope IS NULL OR COALESCE("
            "jsonb_typeof(dependency_scope) = 'object'"
            " AND dependency_scope->>'schema_version' = 'dependency_scope.v1'"
            " AND jsonb_typeof(dependency_scope->'sku_sets') = 'object', false)",
            name="analysis_runs_ck38",
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
        Index(
            "analysis_runs_dependency_scope_gin",
            "dependency_scope",
            postgresql_using="gin",
            postgresql_ops={"dependency_scope": "jsonb_path_ops"},
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
    stale_primary_reason: Mapped[str | None]
    stale_reason_codes: Mapped[list[str] | None] = mapped_column(ARRAY(Text()))
    stale_detected_seq: Mapped[int | None]
    stale_detected_at: Mapped[datetime | None]
    is_latest: Mapped[bool]
    dependency_scope: Mapped[dict[str, Any] | None]
    model_name: Mapped[str | None]
    prompt_version: Mapped[str | None]
    final_explanation_json: Mapped[dict[str, Any] | None]
    explanation_validation_status: Mapped[str | None]
    explanation_generated_at: Mapped[datetime | None]
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
