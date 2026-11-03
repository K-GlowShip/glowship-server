"""align analysis tables and delay_events.episode_no with v6.2 DDL

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-03 15:00:00

C(분석 에이전트) 분석 표 5개와 delay_events 를 v6.2 DDL 과 같게 맞춘다.
열·제약·인덱스 이름과 식은 erd_glowship_postgres_v6_2_analysis_ddl.sql 과 같다(제약 식은 DDL 원문에서 그대로 옮김).
  - analysis_runs: 생성 사유·트리거 스냅샷, 완료 시점 상태·결과 분류, 계산 계약·배분 규칙 버전, 판정 서명 8열
    (ck15~ck26, ck34, ck35, ck37, ck39~ck41), 트리거 스냅샷 FK, analysis_runs_auto_trigger_uq
  - inventory_impact_results: SKU 범위·예측 방식·수요 기준·기준선·raw 값 16열 (ck3~ck19)
  - at_risk_orders: 기준선 배송일·구간 밖·위험 귀속·대기 원인 4열 (ck3~ck6)
  - response_options: 사유 코드·감축량·지연 유발 회복·추천 순위·SKU별 결과 5열 (ck5~ck17), response_options_rank_uq,
    이름 변경 partially_recovered_orders → item_secured_blocked_orders,
    주문 집계 3열 NULL 허용(NULL = 계산 안 함), 대응안 코드 EAST_FULFILL → OTHER_REGION_FULFILL (ck1)
  - option_order_outcomes: 확보 상태·미회복 사유 2열 (ck8~ck12), 이름 변경 allocated_quantity → secured_quantity
    (ck4·ck5 는 열 이름을 따라 바뀜), 결과 코드 FULL·PARTIAL → RECOVERED (ck1), 공급처 EAST_WAREHOUSE → OTHER_REGION_WAREHOUSE (ck7)
  - delay_events: episode_no (ck3, ck6, uk3). 기존 행은 dedupe_key(`{shipment_id}:EP{n}`)의 n 으로 채운다
  - 범위 밖: users 형식 차이(A 영역), run_code(0006), 워커 역할 권한, aux 보조 표,
    2025 v5 합성데이터 적재(make db-seed): 0005 뒤에는 분석 표·delay_events 열이 v5 CSV 와 달라
    seed 스크립트가 원본 파일 검증에서 멈춘다. v6.2 공유 묶음은 DDL + CSV 직접 적재가 기준(README 3절)

v6.2 DDL 로 직접 만든 DB(팀 공통 개발 DB)에는 이미 있으므로 upgrade 대신 `alembic stamp 0005` 로 기준점만 맞춘다.
그런 DB 에서 upgrade 를 하면 시작 전에 멈추고 stamp 를 안내한다(아무것도 바꾸지 않음).

upgrade·downgrade 는 다음 경우 시작 전에 멈춘다(아무것도 바꾸지 않음)
  - 분석 결과 표 5개에 행이 있으면: 새 NOT NULL 열(생성 사유, SKU별 결과 등)을 채울 값이 없고,
    옛 코드값(FULL·PARTIAL, EAST_FULFILL 등)을 새 값으로 기계적으로 바꿀 수 없다. 행을 먼저 정리한다
  - delay_events.dedupe_key 가 `{shipment_id}:EP{n}` 형식이 아닌 행이 있으면(episode_no 를 정할 수 없음)

downgrade 는 서버 마이그레이션(0001→0005)으로 만든 DB 에서만 쓴다. 환경 변수 GLOWSHIP_ALLOW_0005_DOWNGRADE=1 이 없으면 멈춘다.
  - v6.2 DDL 로 만든 DB 도 0005 기록을 가질 수 있는데(stamp), 마이그레이션 기록만으로는 두 DB 를 구분할 수 없다.
    그런 DB 에서 downgrade 하면 DDL 원본 표가 v5 모양(열 삭제·이름 복원·옛 코드값 CHECK)으로 바뀌므로 명시적으로 허용하게 한다.
"""

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ANALYSIS_RESULT_TABLES = (
    "analysis_runs",
    "inventory_impact_results",
    "at_risk_orders",
    "response_options",
    "option_order_outcomes",
)

_ANALYSIS_RUN_CHECKS = {
    "analysis_runs_ck15": "trigger_reason IN ('EPISODE_STARTED','SCHEDULE_WORSENED','SCHEDULE_IMPROVED','STAGE_CHANGED','DELAY_RECOVERED','USER_REANALYSIS')",
    "analysis_runs_ck16": "(trigger_type = 'AUTO') = (trigger_shipment_schedule_snapshot_id IS NOT NULL)",
    "analysis_runs_ck17": "(trigger_type = 'REANALYSIS') = (trigger_reason = 'USER_REANALYSIS')",
    "analysis_runs_ck18": "(version_no = 1) = (trigger_reason = 'EPISODE_STARTED')",
    "analysis_runs_ck19": "completed_status IS NULL OR completed_status IN ('READY_FOR_REVIEW','NO_RISK','NO_DELAY','ON_HOLD','FAILED')",
    "analysis_runs_ck20": "result_class IS NULL OR result_class IN ('READY_ORDER_RISK','FORECAST_ONLY_READY','PURE_NO_RISK','WARNING_NO_RISK')",
    "analysis_runs_ck21": "result_class IS NULL OR COALESCE((completed_status = 'READY_FOR_REVIEW' AND result_class IN ('READY_ORDER_RISK','FORECAST_ONLY_READY')) OR (completed_status = 'NO_RISK' AND result_class IN ('PURE_NO_RISK','WARNING_NO_RISK')), false)",
    "analysis_runs_ck22": "completed_status IS NULL OR completed_status NOT IN ('READY_FOR_REVIEW','NO_RISK') OR result_class IS NOT NULL",
    "analysis_runs_ck23": "status NOT IN ('READY_FOR_REVIEW','NO_RISK','NO_DELAY','ON_HOLD','FAILED') OR completed_status IS NOT DISTINCT FROM status",
    "analysis_runs_ck24": "status <> 'FINALIZED' OR completed_status IS NOT DISTINCT FROM 'READY_FOR_REVIEW'",
    "analysis_runs_ck25": "status <> 'STALE' OR completed_status IS NULL OR completed_status IN ('READY_FOR_REVIEW','NO_RISK')",
    "analysis_runs_ck26": "failure_code IS DISTINCT FROM 'EXPLANATION_GENERATION_FAILED' OR COALESCE(status = 'FAILED' AND failed_step = 'VALIDATING_OUTPUT' AND attempt_count = 3 AND retry_exhausted AND explanation_validation_status = 'FAILED' AND final_explanation_json IS NULL, false)",
    "analysis_runs_ck34": "calculation_contract_version IN ('T14-v1')",
    "analysis_runs_ck35": "allocation_rule_version IN ('D00-B-b2-v1')",
    "analysis_runs_ck37": "status NOT IN ('READY_FOR_REVIEW','NO_RISK','FINALIZED') OR (dependency_scope IS NOT NULL AND decision_signature IS NOT NULL)",
    "analysis_runs_ck39": "decision_signature IS NULL OR COALESCE(jsonb_typeof(decision_signature) = 'object' AND decision_signature->>'schema_version' = 'decision_signature.v1' AND jsonb_typeof(decision_signature->'options') = 'array', false)",
    "analysis_runs_ck40": "(decision_signature IS NULL) = (decision_signature_sha256 IS NULL)",
    "analysis_runs_ck41": "decision_signature_sha256 IS NULL OR decision_signature_sha256 ~ '^[0-9a-f]{64}$'",
}

_INVENTORY_IMPACT_CHECKS = {
    "inventory_impact_results_ck3": "sku_scope IN ('IMPACTED','WATCH','SHIPMENT_ONLY')",
    "inventory_impact_results_ck4": "forecast_method IN ('ORDER_FIRST_APPROX_V1')",
    "inventory_impact_results_ck5": "sales_basis_type IN ('ORDER_DEMAND','OUTBOUND')",
    "inventory_impact_results_ck6": "(sales_basis_start_date IS NULL) = (sales_basis_end_date IS NULL) AND (sales_basis_start_date IS NULL OR sales_basis_end_date - sales_basis_start_date + 1 = sales_basis_days)",
    "inventory_impact_results_ck7": "sales_basis_event_days IS NULL OR (sales_basis_event_days BETWEEN 0 AND sales_basis_days AND (sales_basis_event_days = 0) = (COALESCE(cardinality(sales_basis_event_names), 0) = 0))",
    "inventory_impact_results_ck8": "baseline_planned_3pl_inbound_date <= expected_3pl_inbound_date",
    "inventory_impact_results_ck9": "COALESCE(committed_open_quantity, 0) >= 0 AND COALESCE(raw_baseline_shortage_quantity, 0) >= 0 AND COALESCE(raw_shortage_quantity, 0) >= 0 AND COALESCE(raw_increment_shortage_quantity, 0) >= 0 AND COALESCE(baseline_stockout_days, 0) >= 0 AND COALESCE(baseline_shortage_quantity, 0) >= 0 AND COALESCE(stockout_days, 0) >= 0 AND COALESCE(shortage_quantity, 0) >= 0",
    "inventory_impact_results_ck10": "calculation_status <> 'CALCULATED' OR (committed_open_quantity IS NOT NULL AND raw_baseline_shortage_quantity IS NOT NULL AND raw_shortage_quantity IS NOT NULL AND raw_increment_shortage_quantity IS NOT NULL AND baseline_stockout_days IS NOT NULL AND baseline_shortage_quantity IS NOT NULL AND sales_basis_start_date IS NOT NULL AND sales_basis_end_date IS NOT NULL AND sales_basis_event_days IS NOT NULL)",
    "inventory_impact_results_ck11": "calculation_status <> 'NOT_ESTIMABLE' OR (average_daily_sales IS NULL AND safety_stock_quantity IS NULL AND raw_baseline_shortage_quantity IS NULL AND raw_shortage_quantity IS NULL AND raw_increment_shortage_quantity IS NULL AND baseline_safety_stock_breach_date IS NULL AND baseline_stockout_date IS NULL AND baseline_stockout_days IS NULL AND baseline_shortage_quantity IS NULL AND safety_stock_breach_date IS NULL AND stockout_date IS NULL AND stockout_days IS NULL AND shortage_quantity IS NULL)",
    "inventory_impact_results_ck12": "shortage_quantity IS NULL OR baseline_shortage_quantity IS NULL OR (shortage_quantity >= baseline_shortage_quantity AND stockout_days >= baseline_stockout_days)",
    "inventory_impact_results_ck13": "baseline_stockout_date IS NULL OR (stockout_date IS NOT NULL AND stockout_date <= baseline_stockout_date)",
    "inventory_impact_results_ck14": "baseline_safety_stock_breach_date IS NULL OR (safety_stock_breach_date IS NOT NULL AND safety_stock_breach_date <= baseline_safety_stock_breach_date)",
    "inventory_impact_results_ck15": "calculation_status <> 'CALCULATED' OR ((stockout_date IS NULL) = (stockout_days = 0) AND (baseline_stockout_date IS NULL) = (baseline_stockout_days = 0))",
    "inventory_impact_results_ck16": "(stockout_date IS NULL OR stockout_date < expected_3pl_inbound_date) AND (baseline_stockout_date IS NULL OR baseline_stockout_date < expected_3pl_inbound_date)",
    "inventory_impact_results_ck17": "raw_baseline_shortage_quantity IS NULL OR COALESCE(baseline_shortage_quantity = ceil(raw_baseline_shortage_quantity) AND shortage_quantity = baseline_shortage_quantity + ceil(raw_increment_shortage_quantity), false)",
    "inventory_impact_results_ck18": "shortage_quantity IS NULL OR baseline_shortage_quantity IS NULL OR shortage_quantity = baseline_shortage_quantity OR sku_scope = 'IMPACTED'",
    "inventory_impact_results_ck19": "COALESCE((raw_increment_shortage_quantity IS NULL) = (raw_baseline_shortage_quantity IS NULL) AND (raw_increment_shortage_quantity IS NULL) = (raw_shortage_quantity IS NULL) AND (raw_increment_shortage_quantity IS NULL OR abs(raw_increment_shortage_quantity - greatest(raw_shortage_quantity - raw_baseline_shortage_quantity, 0)) <= 0.0001), false)",
}

_AT_RISK_ORDER_CHECKS = {
    "at_risk_orders_ck3": "risk_attribution IN ('PRE_EXISTING','DELAY_INDUCED')",
    "at_risk_orders_ck4": "wait_cause IS NULL OR wait_cause IN ('DIRECT_SKU_SHORTAGE','BUNDLED_ORDER_WAIT')",
    "at_risk_orders_ck5": "risk_attribution <> 'PRE_EXISTING' OR COALESCE(baseline_expected_delivery_at > promised_delivery_at, false)",
    "at_risk_orders_ck6": "risk_attribution <> 'DELAY_INDUCED' OR baseline_expected_delivery_at IS NULL OR baseline_expected_delivery_at <= promised_delivery_at",
}

_RESPONSE_OPTION_CHECKS = {
    "response_options_ck5": "(feasibility_status = 'FEASIBLE') = (reason_code IS NULL)",
    "response_options_ck6": "feasibility_status <> 'NOT_FEASIBLE' OR reason_code IN ('ARRIVES_AFTER_DELAYED_SHIPMENT','PROMISE_UNREACHABLE_LEADTIME','NO_KR_ELIGIBLE_STOCK','NO_TRANSFERABLE_STOCK','FORECAST_EFFECT_BELOW_ONE_UNIT','NO_RECOVERY_EFFECT')",
    "response_options_ck7": "feasibility_status <> 'NEEDS_CHECK' OR reason_code IN ('DONOR_NO_NEXT_INBOUND','MISSING_OPERATING_RULE','MISSING_REQUIRED_DATA')",
    "response_options_ck8": "(fully_recovered_orders IS NULL) = (not_recovered_orders IS NULL) AND (fully_recovered_orders IS NULL) = (item_secured_blocked_orders IS NULL) AND (fully_recovered_orders IS NULL) = (delay_induced_recovered_orders IS NULL)",
    "response_options_ck9": "feasibility_status <> 'FEASIBLE' OR (fully_recovered_orders IS NOT NULL AND reduction_committed_total IS NOT NULL)",
    "response_options_ck10": "fully_recovered_orders IS NULL OR (fully_recovered_orders >= 0 AND not_recovered_orders >= 0 AND item_secured_blocked_orders BETWEEN 0 AND not_recovered_orders AND delay_induced_recovered_orders BETWEEN 0 AND fully_recovered_orders)",
    "response_options_ck11": "required_quantity >= 0 AND COALESCE(securable_quantity, 0) >= 0 AND COALESCE(reduction_committed_total, 0) >= 0",
    "response_options_ck12": "feasibility_status <> 'FEASIBLE' OR option_type = 'KEEP_PLAN' OR delay_induced_recovered_orders >= 1 OR reduction_committed_total >= 1",
    "response_options_ck13": "reason_code IS DISTINCT FROM 'FORECAST_EFFECT_BELOW_ONE_UNIT' OR COALESCE(reduction_committed_total = 0 AND COALESCE(delay_induced_recovered_orders, 0) = 0, false)",
    "response_options_ck14": "option_type <> 'KEEP_PLAN' OR (COALESCE(reduction_committed_total, 0) = 0 AND COALESCE(delay_induced_recovered_orders, 0) = 0)",
    "response_options_ck15": "recommendation_rank IS NULL OR (recommendation_rank >= 1 AND feasibility_status = 'FEASIBLE')",
    "response_options_ck16": "is_recommended = COALESCE(recommendation_rank = 1, false)",
    "response_options_ck17": "COALESCE(jsonb_typeof(sku_results) = 'object' AND sku_results->>'schema_version' = 'sku_results.v1' AND sku_results->>'unit' = 'EA' AND jsonb_typeof(sku_results->'items') = 'array', false)",
}

_OPTION_ORDER_OUTCOME_CHECKS = {
    "option_order_outcomes_ck8": "line_securement_status IN ('FULLY_SECURED','PARTIALLY_SECURED','NOT_SECURED')",
    "option_order_outcomes_ck9": "outcome <> 'RECOVERED' OR line_securement_status = 'FULLY_SECURED'",
    "option_order_outcomes_ck10": "(line_securement_status = 'NOT_SECURED') = (secured_quantity = 0)",
    "option_order_outcomes_ck11": "(outcome = 'RECOVERED') = (not_recovered_reason IS NULL)",
    "option_order_outcomes_ck12": "not_recovered_reason IS NULL OR not_recovered_reason IN ('PROMISE_UNREACHABLE_LEADTIME','NO_KR_ELIGIBLE_STOCK','NO_TRANSFERABLE_STOCK','DONOR_NO_NEXT_INBOUND','NO_RECOVERY_EFFECT')",
}

_DELAY_EVENT_CHECKS = {
    "delay_events_ck3": "episode_no > 0",
    "delay_events_ck6": "dedupe_key = shipment_id::text || '\\:EP' || episode_no::text",
}

_REPLACED_CHECKS_V62 = {
    "response_options_ck1": "option_type IN ('KEEP_PLAN','PRE_DEPARTURE_AIR','KR_AIR_REPLENISH','OTHER_REGION_FULFILL')",
    "option_order_outcomes_ck1": "outcome IN ('RECOVERED','NOT_RECOVERED')",
    "option_order_outcomes_ck7": "supply_source IS NULL OR supply_source IN ('EXISTING_STOCK','OTHER_REGION_WAREHOUSE','KR_AIR','PRE_DEPARTURE_AIR')",
}

_REPLACED_CHECKS_V5 = {
    "response_options_ck1": "option_type IN ('KEEP_PLAN','PRE_DEPARTURE_AIR','KR_AIR_REPLENISH','EAST_FULFILL')",
    "option_order_outcomes_ck1": "outcome IN ('FULL','PARTIAL','NOT_RECOVERED')",
    "option_order_outcomes_ck7": "supply_source IS NULL OR supply_source IN ('EXISTING_STOCK','EAST_WAREHOUSE','KR_AIR','PRE_DEPARTURE_AIR')",
}


def _columns() -> dict[str, list[sa.Column]]:
    """표별로 더할 열 (v6.2 DDL 과 같은 형식·NULL·기본값)"""
    return {
        "analysis_runs": [
            sa.Column("trigger_reason", sa.Text(), nullable=False),
            sa.Column(
                "trigger_shipment_schedule_snapshot_id", sa.UUID(), nullable=True
            ),
            sa.Column("completed_status", sa.Text(), nullable=True),
            sa.Column("result_class", sa.Text(), nullable=True),
            sa.Column("calculation_contract_version", sa.Text(), nullable=False),
            sa.Column("allocation_rule_version", sa.Text(), nullable=False),
            sa.Column(
                "decision_signature",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=True,
            ),
            sa.Column("decision_signature_sha256", sa.Text(), nullable=True),
        ],
        "inventory_impact_results": [
            sa.Column("sku_scope", sa.Text(), nullable=False),
            sa.Column("forecast_method", sa.Text(), nullable=False),
            sa.Column("committed_open_quantity", sa.Integer(), nullable=True),
            sa.Column("sales_basis_type", sa.Text(), nullable=False),
            sa.Column("sales_basis_start_date", sa.Date(), nullable=True),
            sa.Column("sales_basis_end_date", sa.Date(), nullable=True),
            sa.Column("sales_basis_event_days", sa.Integer(), nullable=True),
            sa.Column(
                "sales_basis_event_names", postgresql.ARRAY(sa.Text()), nullable=True
            ),
            sa.Column("baseline_planned_3pl_inbound_date", sa.Date(), nullable=False),
            sa.Column(
                "raw_baseline_shortage_quantity", sa.Numeric(12, 4), nullable=True
            ),
            sa.Column("raw_shortage_quantity", sa.Numeric(12, 4), nullable=True),
            sa.Column("baseline_safety_stock_breach_date", sa.Date(), nullable=True),
            sa.Column("baseline_stockout_date", sa.Date(), nullable=True),
            sa.Column("baseline_stockout_days", sa.Integer(), nullable=True),
            sa.Column("baseline_shortage_quantity", sa.Integer(), nullable=True),
            sa.Column(
                "raw_increment_shortage_quantity", sa.Numeric(12, 4), nullable=True
            ),
        ],
        "at_risk_orders": [
            sa.Column(
                "baseline_expected_delivery_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
            sa.Column(
                "expected_beyond_horizon",
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=False,
            ),
            sa.Column("risk_attribution", sa.Text(), nullable=False),
            sa.Column("wait_cause", sa.Text(), nullable=True),
        ],
        "response_options": [
            sa.Column("reason_code", sa.Text(), nullable=True),
            sa.Column("reduction_committed_total", sa.Integer(), nullable=True),
            sa.Column("delay_induced_recovered_orders", sa.Integer(), nullable=True),
            sa.Column("recommendation_rank", sa.Integer(), nullable=True),
            sa.Column(
                "sku_results", postgresql.JSONB(astext_type=sa.Text()), nullable=False
            ),
        ],
        "option_order_outcomes": [
            sa.Column("not_recovered_reason", sa.Text(), nullable=True),
            sa.Column("line_securement_status", sa.Text(), nullable=False),
        ],
    }


_CHECKS_BY_TABLE = {
    "analysis_runs": _ANALYSIS_RUN_CHECKS,
    "inventory_impact_results": _INVENTORY_IMPACT_CHECKS,
    "at_risk_orders": _AT_RISK_ORDER_CHECKS,
    "response_options": _RESPONSE_OPTION_CHECKS,
    "option_order_outcomes": _OPTION_ORDER_OUTCOME_CHECKS,
}

# 대응안 주문 집계: NULL = 계산 안 함 (DDL response_options_ck8)
_NULLABLE_COUNTS = ("fully_recovered_orders", "not_recovered_orders")

# downgrade 를 허용하는 환경 변수 (위 docstring 참고)
_ALLOW_DOWNGRADE_ENV = "GLOWSHIP_ALLOW_0005_DOWNGRADE"

# ":EP" 의 콜론은 sa.text() 가 바인드 변수로 읽으므로 정규식에서는 [:] 로 쓴다
_EPISODE_KEY = r"^[0-9a-f-]{36}[:]EP[1-9][0-9]*$"


def _scalar(sql: str):
    return op.get_bind().execute(sa.text(sql)).scalar()


def _stop_if_analysis_rows(action: str) -> None:
    counts = {
        table: _scalar(f"SELECT count(*) FROM {table}")
        for table in _ANALYSIS_RESULT_TABLES
    }
    filled = {table: n for table, n in counts.items() if n}
    if filled:
        hint = (
            " v6.2 DDL 로 만든 DB 라면 upgrade 대신 `alembic stamp 0005` 를 쓰세요."
            if action == "upgrade"
            else ""
        )
        raise RuntimeError(
            f"0005 {action}: 분석 결과 표에 행이 있어 멈춥니다 {filled}. "
            "NOT NULL 열을 채우거나 코드값을 바꿀 수 없으므로 행을 먼저 정리하세요."
            + hint
        )


def upgrade() -> None:
    if _scalar(
        "SELECT count(*) FROM information_schema.columns"
        " WHERE table_schema = current_schema() AND table_name = 'analysis_runs'"
        " AND column_name = 'trigger_reason'"
    ):
        raise RuntimeError(
            "0005 upgrade: 이미 v6.2 분석 표 열(analysis_runs.trigger_reason)이 있습니다. "
            "v6.2 DDL 로 만든 DB 는 `alembic stamp 0005` 로 기준점만 맞추세요."
        )
    _stop_if_analysis_rows("upgrade")
    bad_keys = _scalar(
        f"SELECT count(*) FROM delay_events WHERE dedupe_key !~ '{_EPISODE_KEY}'"
        " OR split_part(dedupe_key, ':', 1) <> shipment_id::text"
    )
    if bad_keys:
        raise RuntimeError(
            f"0005 upgrade: dedupe_key 가 '{{shipment_id}}:EP{{n}}' 형식이 아닌 지연 이벤트 {bad_keys}건이 있어 "
            "episode_no 를 정할 수 없습니다. 먼저 고치세요."
        )

    # delay_events.episode_no: 기존 행은 dedupe_key 의 EP 번호로 채운 뒤 NOT NULL
    op.add_column("delay_events", sa.Column("episode_no", sa.Integer(), nullable=True))
    op.execute(
        "UPDATE delay_events SET episode_no = substring(dedupe_key from '[:]EP([0-9]+)$')::integer"
    )
    op.alter_column("delay_events", "episode_no", nullable=False)
    for name, condition in _DELAY_EVENT_CHECKS.items():
        op.create_check_constraint(name, "delay_events", condition)
    op.create_unique_constraint(
        "delay_events_uk3", "delay_events", ["company_id", "shipment_id", "episode_no"]
    )

    # 이름 변경과 NULL 허용 (분석 결과 행이 없음을 위에서 확인)
    op.alter_column(
        "response_options",
        "partially_recovered_orders",
        new_column_name="item_secured_blocked_orders",
        nullable=True,
    )
    for column in _NULLABLE_COUNTS:
        op.alter_column("response_options", column, nullable=True)
    op.alter_column(
        "option_order_outcomes",
        "allocated_quantity",
        new_column_name="secured_quantity",
    )

    for table, columns in _columns().items():
        for column in columns:
            op.add_column(table, column)

    for name, condition in _REPLACED_CHECKS_V62.items():
        table = name.rsplit("_ck", 1)[0]
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, condition)
    for table, checks in _CHECKS_BY_TABLE.items():
        for name, condition in checks.items():
            op.create_check_constraint(name, table, condition)

    op.create_foreign_key(
        "fk_analysis_runs__trigger_shipment_schedule_snapshot_id",
        "analysis_runs",
        "shipment_schedule_snapshots",
        ["company_id", "trigger_shipment_schedule_snapshot_id"],
        ["company_id", "shipment_schedule_snapshot_id"],
    )
    op.create_index(
        "analysis_runs_auto_trigger_uq",
        "analysis_runs",
        ["delay_event_id", "trigger_shipment_schedule_snapshot_id"],
        unique=True,
        postgresql_where=sa.text("trigger_type = 'AUTO'"),
    )
    op.create_index(
        "response_options_rank_uq",
        "response_options",
        ["analysis_run_id", "recommendation_rank"],
        unique=True,
        postgresql_where=sa.text("recommendation_rank IS NOT NULL"),
    )


def downgrade() -> None:
    if os.environ.get(_ALLOW_DOWNGRADE_ENV) != "1":
        raise RuntimeError(
            "0005 downgrade: 분석 표를 v5 모양으로 되돌립니다. 서버 마이그레이션(0001→0005)으로 만든 DB 에서만 쓰세요. "
            "v6.2 DDL 로 만든 DB(팀 공통 개발 DB)에서는 DDL 원본 표가 바뀝니다. "
            f"진행하려면 {_ALLOW_DOWNGRADE_ENV}=1 을 주고 다시 실행하세요."
        )
    _stop_if_analysis_rows("downgrade")

    op.drop_index("response_options_rank_uq", table_name="response_options")
    op.drop_index("analysis_runs_auto_trigger_uq", table_name="analysis_runs")
    op.drop_constraint(
        "fk_analysis_runs__trigger_shipment_schedule_snapshot_id",
        "analysis_runs",
        type_="foreignkey",
    )
    for table, checks in reversed(_CHECKS_BY_TABLE.items()):
        for name in reversed(checks):
            op.drop_constraint(name, table, type_="check")
    for name, condition in _REPLACED_CHECKS_V5.items():
        table = name.rsplit("_ck", 1)[0]
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, condition)

    for table, columns in reversed(_columns().items()):
        for column in reversed(columns):
            op.drop_column(table, column.name)

    op.alter_column(
        "option_order_outcomes",
        "secured_quantity",
        new_column_name="allocated_quantity",
    )
    for column in _NULLABLE_COUNTS:
        op.alter_column("response_options", column, nullable=False)
    op.alter_column(
        "response_options",
        "item_secured_blocked_orders",
        new_column_name="partially_recovered_orders",
        nullable=False,
    )

    op.drop_constraint("delay_events_uk3", "delay_events", type_="unique")
    for name in reversed(_DELAY_EVENT_CHECKS):
        op.drop_constraint(name, "delay_events", type_="check")
    op.drop_column("delay_events", "episode_no")
