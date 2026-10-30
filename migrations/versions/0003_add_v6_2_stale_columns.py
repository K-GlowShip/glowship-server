"""add v6.2 columns for STALE judgment

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-30 18:00:00

STALE 판정(D-13 의존 입력 기반, 정책 결정 ST-3)에 필요한 v6.2 열·제약을 더한다.
  - analysis_runs: STALE 사유 4열, dependency_scope (ck27~ck33, ck36, ck38)
  - data_change_records: changed_fields (ck5) — SKU 속성 변경 판정에 사용
  - delay_events: closed_at, close_reason (ck4, ck5, ck7) — 종료된 지연의 실행 동결에 사용
  - 열·제약 이름과 식은 erd_glowship_postgres_v6_2_analysis_ddl.sql 과 같다
  - v6.2 DDL로 직접 만든 DB에는 이미 있으므로 upgrade 대신 `alembic stamp head` 로 기준점만 맞춘다
  - upgrade 는 기존 STALE 행이 있으면 ck27(주 사유 필수)에 걸려 실패한다
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REASONS = (
    "'DELAY_SCHEDULE_CHANGED','DELAY_STAGE_CHANGED','RELEVANT_ORDER_CHANGED',"
    "'RELEVANT_INVENTORY_CHANGED','RELEVANT_INBOUND_CHANGED','OPERATING_RULE_CHANGED',"
    "'RELEVANT_SKU_ATTRIBUTE_CHANGED','TIME_ELAPSED'"
)

_CHECKS = {
    "analysis_runs_ck27": "(status = 'STALE') = (stale_primary_reason IS NOT NULL)",
    "analysis_runs_ck28": "stale_primary_reason IS NULL OR stale_primary_reason IN"
    f" ({_REASONS},'MULTIPLE_RELEVANT_CHANGES')",
    "analysis_runs_ck29": "(stale_primary_reason IS NULL) = (stale_reason_codes IS NULL)"
    " AND (stale_primary_reason IS NULL) = (stale_detected_at IS NULL)",
    "analysis_runs_ck30": "stale_reason_codes IS NULL"
    f" OR stale_reason_codes <@ ARRAY[{_REASONS}]::text[]",
    "analysis_runs_ck31": "stale_reason_codes IS NULL"
    " OR (stale_primary_reason = 'MULTIPLE_RELEVANT_CHANGES')"
    " = (cardinality(stale_reason_codes) >= 2)",
    "analysis_runs_ck32": "stale_primary_reason IS NULL"
    " OR stale_primary_reason = 'MULTIPLE_RELEVANT_CHANGES'"
    " OR stale_reason_codes = ARRAY[stale_primary_reason]",
    "analysis_runs_ck33": "stale_primary_reason IS NULL"
    " OR (stale_primary_reason = 'TIME_ELAPSED') = (stale_detected_seq IS NULL)",
    "analysis_runs_ck36": "status NOT IN ('ON_HOLD','FAILED') OR dependency_scope IS NULL",
    "analysis_runs_ck38": "dependency_scope IS NULL OR COALESCE("
    "jsonb_typeof(dependency_scope) = 'object'"
    " AND dependency_scope->>'schema_version' = 'dependency_scope.v1'"
    " AND jsonb_typeof(dependency_scope->'sku_sets') = 'object', false)",
}

_DELAY_EVENT_CHECKS = {
    "delay_events_ck4": "(closed_at IS NULL) = (close_reason IS NULL)",
    "delay_events_ck5": "close_reason IS NULL OR close_reason IN ('RECOVERED','RECEIVED')",
    "delay_events_ck7": "closed_at IS NULL OR closed_at >= detected_at",
}


def upgrade() -> None:
    op.add_column(
        "analysis_runs", sa.Column("stale_primary_reason", sa.Text(), nullable=True)
    )
    op.add_column(
        "analysis_runs",
        sa.Column("stale_reason_codes", postgresql.ARRAY(sa.Text()), nullable=True),
    )
    op.add_column(
        "analysis_runs", sa.Column("stale_detected_seq", sa.Integer(), nullable=True)
    )
    op.add_column(
        "analysis_runs",
        sa.Column("stale_detected_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "analysis_runs",
        sa.Column(
            "dependency_scope", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
    )
    for name, condition in _CHECKS.items():
        op.create_check_constraint(name, "analysis_runs", condition)
    op.create_index(
        "analysis_runs_dependency_scope_gin",
        "analysis_runs",
        ["dependency_scope"],
        postgresql_using="gin",
        postgresql_ops={"dependency_scope": "jsonb_path_ops"},
    )

    op.add_column(
        "data_change_records",
        sa.Column("changed_fields", postgresql.ARRAY(sa.Text()), nullable=True),
    )
    op.create_check_constraint(
        "data_change_records_ck5",
        "data_change_records",
        "changed_fields IS NULL"
        " OR (change_type = 'UPDATE' AND cardinality(changed_fields) >= 1)",
    )

    op.add_column(
        "delay_events",
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("delay_events", sa.Column("close_reason", sa.Text(), nullable=True))
    for name, condition in _DELAY_EVENT_CHECKS.items():
        op.create_check_constraint(name, "delay_events", condition)


def downgrade() -> None:
    for name in reversed(_DELAY_EVENT_CHECKS):
        op.drop_constraint(name, "delay_events", type_="check")
    op.drop_column("delay_events", "close_reason")
    op.drop_column("delay_events", "closed_at")

    op.drop_constraint("data_change_records_ck5", "data_change_records", type_="check")
    op.drop_column("data_change_records", "changed_fields")

    op.drop_index("analysis_runs_dependency_scope_gin", table_name="analysis_runs")
    for name in reversed(_CHECKS):
        op.drop_constraint(name, "analysis_runs", type_="check")
    for column in (
        "dependency_scope",
        "stale_detected_at",
        "stale_detected_seq",
        "stale_reason_codes",
        "stale_primary_reason",
    ):
        op.drop_column("analysis_runs", column)
