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
from app.modules.delay_analysis.infrastructure.analysis_enums import StaleReasonCode

# STALE 사유 코드 목록 (stale_reason_codes 원소, analysis_enums.StaleReasonCode)
_STALE_REASON_SQL = ",".join(f"'{code}'" for code in StaleReasonCode)


class AnalysisRun(Base):
    """
    지연 이벤트 영향 분석 실행 (버전별, 재분석 시 parent_analysis_run_id로 연결)
      - delay_event별 is_latest 행은 하나만 존재합니다 (부분 고유 인덱스).
      - FAILED·ON_HOLD 상태일 때만 실패 요약 5컬럼(failed_step ~ failure_message)을 채웁니다.
      - STALE 상태일 때만 STALE 사유 4컬럼(stale_*)을 채웁니다. TIME_ELAPSED 만 stale_detected_seq 가 비어 있습니다.
      - dependency_scope 는 분석이 실제로 읽은 입력 범위(dependency_scope.v1)로, STALE 1차 판정에 씁니다.
      - run_code 는 화면에 보이는 실행 ID(D14, `AN-YYMMDD-NNN`)로 고객사 안에서 고유합니다(0006).
    """

    __tablename__ = "analysis_runs"
    __table_args__ = (
        UniqueConstraint("company_id", "analysis_run_id", name="analysis_runs_uk1"),
        UniqueConstraint("delay_event_id", "version_no", name="analysis_runs_uk2"),
        # 0006(D14): 표시용 실행 ID. v6.2 DDL 에는 없고 서버 리비전이 추가한다
        UniqueConstraint("company_id", "run_code", name="analysis_runs_run_code_uk"),
        CheckConstraint(
            "run_code ~ '^AN-[0-9]{6}-[0-9]{3}$'", name="analysis_runs_run_code_ck"
        ),
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
        # v6.2(0005): 생성 사유·완료 상태·결과 분류·계산 계약·판정 서명
        CheckConstraint(
            "trigger_reason IN ('EPISODE_STARTED','SCHEDULE_WORSENED','SCHEDULE_IMPROVED','STAGE_CHANGED','DELAY_RECOVERED','USER_REANALYSIS')",
            name="analysis_runs_ck15",
        ),
        CheckConstraint(
            "(trigger_type = 'AUTO') = (trigger_shipment_schedule_snapshot_id IS NOT NULL)",
            name="analysis_runs_ck16",
        ),
        CheckConstraint(
            "(trigger_type = 'REANALYSIS') = (trigger_reason = 'USER_REANALYSIS')",
            name="analysis_runs_ck17",
        ),
        CheckConstraint(
            "(version_no = 1) = (trigger_reason = 'EPISODE_STARTED')",
            name="analysis_runs_ck18",
        ),
        CheckConstraint(
            "completed_status IS NULL OR completed_status IN ('READY_FOR_REVIEW','NO_RISK','NO_DELAY','ON_HOLD','FAILED')",
            name="analysis_runs_ck19",
        ),
        CheckConstraint(
            "result_class IS NULL OR result_class IN ('READY_ORDER_RISK','FORECAST_ONLY_READY','PURE_NO_RISK','WARNING_NO_RISK')",
            name="analysis_runs_ck20",
        ),
        CheckConstraint(
            "result_class IS NULL OR COALESCE((completed_status = 'READY_FOR_REVIEW' AND result_class IN ('READY_ORDER_RISK','FORECAST_ONLY_READY')) OR (completed_status = 'NO_RISK' AND result_class IN ('PURE_NO_RISK','WARNING_NO_RISK')), false)",
            name="analysis_runs_ck21",
        ),
        CheckConstraint(
            "completed_status IS NULL OR completed_status NOT IN ('READY_FOR_REVIEW','NO_RISK') OR result_class IS NOT NULL",
            name="analysis_runs_ck22",
        ),
        CheckConstraint(
            "status NOT IN ('READY_FOR_REVIEW','NO_RISK','NO_DELAY','ON_HOLD','FAILED') OR completed_status IS NOT DISTINCT FROM status",
            name="analysis_runs_ck23",
        ),
        CheckConstraint(
            "status <> 'FINALIZED' OR completed_status IS NOT DISTINCT FROM 'READY_FOR_REVIEW'",
            name="analysis_runs_ck24",
        ),
        CheckConstraint(
            "status <> 'STALE' OR completed_status IS NULL OR completed_status IN ('READY_FOR_REVIEW','NO_RISK')",
            name="analysis_runs_ck25",
        ),
        CheckConstraint(
            "failure_code IS DISTINCT FROM 'EXPLANATION_GENERATION_FAILED' OR COALESCE(status = 'FAILED' AND failed_step = 'VALIDATING_OUTPUT' AND attempt_count = 3 AND retry_exhausted AND explanation_validation_status = 'FAILED' AND final_explanation_json IS NULL, false)",
            name="analysis_runs_ck26",
        ),
        CheckConstraint(
            "calculation_contract_version IN ('T14-v1')",
            name="analysis_runs_ck34",
        ),
        CheckConstraint(
            "allocation_rule_version IN ('D00-B-b2-v1')",
            name="analysis_runs_ck35",
        ),
        CheckConstraint(
            "status NOT IN ('READY_FOR_REVIEW','NO_RISK','FINALIZED') OR (dependency_scope IS NOT NULL AND decision_signature IS NOT NULL)",
            name="analysis_runs_ck37",
        ),
        CheckConstraint(
            "decision_signature IS NULL OR COALESCE(jsonb_typeof(decision_signature) = 'object' AND decision_signature->>'schema_version' = 'decision_signature.v1' AND jsonb_typeof(decision_signature->'options') = 'array', false)",
            name="analysis_runs_ck39",
        ),
        CheckConstraint(
            "(decision_signature IS NULL) = (decision_signature_sha256 IS NULL)",
            name="analysis_runs_ck40",
        ),
        CheckConstraint(
            "decision_signature_sha256 IS NULL OR decision_signature_sha256 ~ '^[0-9a-f]{64}$'",
            name="analysis_runs_ck41",
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
        ForeignKeyConstraint(
            ["company_id", "trigger_shipment_schedule_snapshot_id"],
            [
                "shipment_schedule_snapshots.company_id",
                "shipment_schedule_snapshots.shipment_schedule_snapshot_id",
            ],
            name="fk_analysis_runs__trigger_shipment_schedule_snapshot_id",
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
        # v6.2(0005): 같은 지연·같은 트리거 스냅샷의 AUTO 실행은 하나만
        Index(
            "analysis_runs_auto_trigger_uq",
            "delay_event_id",
            "trigger_shipment_schedule_snapshot_id",
            unique=True,
            postgresql_where=text("trigger_type = 'AUTO'"),
        ),
    )

    analysis_run_id: Mapped[uuid_pk]
    company_id: Mapped[uuid.UUID]
    run_code: Mapped[str]
    delay_event_id: Mapped[uuid.UUID]
    version_no: Mapped[int]
    parent_analysis_run_id: Mapped[uuid.UUID | None]
    operating_rule_set_id: Mapped[uuid.UUID]
    input_seq: Mapped[int]
    trigger_type: Mapped[str]
    trigger_reason: Mapped[str]
    trigger_shipment_schedule_snapshot_id: Mapped[uuid.UUID | None]
    requested_by_user_id: Mapped[uuid.UUID | None]
    request_reason: Mapped[str | None]
    requested_at: Mapped[datetime | None]
    status: Mapped[str]
    status_reason: Mapped[str | None]
    completed_status: Mapped[str | None]
    result_class: Mapped[str | None]
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
    calculation_contract_version: Mapped[str]
    allocation_rule_version: Mapped[str]
    dependency_scope: Mapped[dict[str, Any] | None]
    decision_signature: Mapped[dict[str, Any] | None]
    decision_signature_sha256: Mapped[str | None]
    model_name: Mapped[str | None]
    prompt_version: Mapped[str | None]
    final_explanation_json: Mapped[dict[str, Any] | None]
    explanation_validation_status: Mapped[str | None]
    explanation_generated_at: Mapped[datetime | None]
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
