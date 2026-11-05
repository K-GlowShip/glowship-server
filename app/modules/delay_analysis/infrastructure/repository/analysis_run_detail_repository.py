import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import UUID, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

"""
    분석 실행 상세 조회 (API 17, 명세 v6.2 4.5절 GET /analysis-runs/{runId})
      - 저장된 실행·결과 행을 그대로 읽습니다. 계산·상태 판단은 하지 않습니다(저장된 결과 읽기, GLOW-115).
      - 모든 조회는 고객사(company_id)로 거릅니다. 로컬 superuser 접속은 RLS 를 우회하므로 조건을 직접 겁니다.
        다른 고객사 실행은 "없는 실행"과 똑같이 None 을 돌려줍니다(존재를 드러내지 않음).
      - 대상(선적·창고·대표 상품·경로·지연 일수)은 여기서 읽지 않고 지연 조회 저장소(4.4)와 같은 함수(D13)로 만듭니다.
      - 응답에 넣지 않는 열(dependency_scope 전체, decision_signature*, raw_*_shortage_quantity, response_options.reason)은 읽지 않습니다.
        dependency_scope 에서는 기준일(base_date)만 꺼냅니다.
"""

_UUID = UUID(as_uuid=True)

_RUN = text(
    """
    SELECT r.analysis_run_id, r.run_code, r.delay_event_id, r.version_no,
           r.parent_analysis_run_id, r.is_latest, r.status, r.completed_status,
           r.result_class, r.status_reason, r.trigger_type, r.trigger_reason,
           r.requested_by_user_id, ru.display_name AS requested_by_display_name,
           ru.role_code::text AS requested_by_role_code,
           r.request_reason, r.requested_at,
           r.started_at, r.finished_at,
           r.stale_detected_at, r.stale_primary_reason, r.stale_reason_codes,
           r.input_seq, CAST(r.dependency_scope->>'base_date' AS date) AS base_date,
           r.operating_rule_set_id, rs.version_no AS operating_rule_version_no,
           r.calculation_contract_version, r.allocation_rule_version,
           r.final_explanation_json, r.explanation_validation_status,
           r.explanation_generated_at, r.model_name, r.prompt_version,
           r.failed_step, r.failure_code, r.attempt_count, r.retry_exhausted,
           r.failure_message
      FROM analysis_runs r
      JOIN operating_rule_sets rs
        ON rs.company_id = r.company_id
       AND rs.operating_rule_set_id = r.operating_rule_set_id
      LEFT JOIN users ru
        ON ru.company_id = r.company_id AND ru.user_id = r.requested_by_user_id
     WHERE r.company_id = :company_id AND r.analysis_run_id = :run_id
    """
).bindparams(bindparam("company_id", type_=_UUID), bindparam("run_id", type_=_UUID))

_DATA_VERSIONS = text(
    """
    SELECT d.data_sync_run_id, d.source_domain, d.finished_at
      FROM analysis_run_data_versions v
      JOIN data_sync_runs d
        ON d.company_id = v.company_id AND d.data_sync_run_id = v.data_sync_run_id
     WHERE v.company_id = :company_id AND v.analysis_run_id = :run_id
     ORDER BY d.source_domain, d.finished_at, d.data_sync_run_id
    """
).bindparams(bindparam("company_id", type_=_UUID), bindparam("run_id", type_=_UUID))

_IMPACTS = text(
    """
    SELECT i.sku_id, k.sku_code, k.product_name, i.warehouse_id, w.warehouse_code,
           w.region_code,
           i.sku_scope, i.calculation_status, i.forecast_method,
           i.available_quantity, i.committed_open_quantity,
           i.sales_basis_type, i.sales_basis_days, i.sales_basis_start_date,
           i.sales_basis_end_date, i.sales_basis_event_days, i.sales_basis_event_names,
           i.average_daily_sales, i.safety_stock_days, i.safety_stock_quantity,
           i.baseline_planned_3pl_inbound_date, i.expected_3pl_inbound_date,
           i.baseline_safety_stock_breach_date, i.baseline_stockout_date,
           i.baseline_stockout_days, i.baseline_shortage_quantity,
           i.safety_stock_breach_date, i.stockout_date, i.stockout_days,
           i.shortage_quantity
      FROM inventory_impact_results i
      JOIN skus k ON k.company_id = i.company_id AND k.sku_id = i.sku_id
      JOIN warehouses w ON w.company_id = i.company_id AND w.warehouse_id = i.warehouse_id
     WHERE i.company_id = :company_id AND i.analysis_run_id = :run_id
     ORDER BY k.sku_code, w.warehouse_code, i.inventory_impact_result_id
    """
).bindparams(bindparam("company_id", type_=_UUID), bindparam("run_id", type_=_UUID))

# 위험 주문은 주문 상세 1줄이 1행입니다(D21). 서비스가 주문별로 묶으므로 주문·줄 번호 순으로 읽습니다.
_AT_RISK_LINES = text(
    """
    SELECT o.at_risk_order_id, l.order_id, ord.order_code, o.order_line_id, l.line_no,
           l.sku_id, k.sku_code, k.product_name, o.open_quantity, o.wait_cause,
           o.risk_attribution, o.promised_delivery_at, o.baseline_expected_delivery_at,
           o.expected_delivery_at, o.expected_beyond_horizon, o.risk_reason
      FROM at_risk_orders o
      JOIN order_lines l ON l.company_id = o.company_id AND l.order_line_id = o.order_line_id
      JOIN orders ord ON ord.company_id = l.company_id AND ord.order_id = l.order_id
      JOIN skus k ON k.company_id = l.company_id AND k.sku_id = l.sku_id
     WHERE o.company_id = :company_id AND o.analysis_run_id = :run_id
     ORDER BY ord.order_code, l.order_id, l.line_no, o.at_risk_order_id
    """
).bindparams(bindparam("company_id", type_=_UUID), bindparam("run_id", type_=_UUID))

# 대응안은 고정 순서(KEEP_PLAN → 출항 전 항공 → 한국 창고 항공 → 다른 권역)로 둡니다. 설명의 option_explanations 와 같은 순서입니다.
_OPTIONS = text(
    """
    SELECT response_option_id, option_type, feasibility_status, reason_code,
           required_quantity, securable_quantity, reduction_committed_total,
           expected_delivery_date, fully_recovered_orders, delay_induced_recovered_orders,
           item_secured_blocked_orders, not_recovered_orders, recommendation_rank,
           is_recommended, sku_results
      FROM response_options
     WHERE company_id = :company_id AND analysis_run_id = :run_id
     ORDER BY array_position(
                ARRAY['KEEP_PLAN','PRE_DEPARTURE_AIR','KR_AIR_REPLENISH','OTHER_REGION_FULFILL'],
                option_type::text),
              response_option_id
    """
).bindparams(bindparam("company_id", type_=_UUID), bindparam("run_id", type_=_UUID))

_OUTCOMES = text(
    """
    SELECT x.response_option_id, x.at_risk_order_id, x.outcome, x.not_recovered_reason,
           x.line_securement_status, x.secured_quantity, x.recovered_quantity,
           x.expected_delivery_at, x.source_warehouse_id,
           w.warehouse_code AS source_warehouse_code,
           w.region_code AS source_region_code, x.supply_source
      FROM option_order_outcomes x
      LEFT JOIN warehouses w
        ON w.company_id = x.company_id AND w.warehouse_id = x.source_warehouse_id
     WHERE x.company_id = :company_id AND x.analysis_run_id = :run_id
     ORDER BY x.response_option_id, x.at_risk_order_id
    """
).bindparams(bindparam("company_id", type_=_UUID), bindparam("run_id", type_=_UUID))

_DRAFTS = text(
    """
    SELECT d.member_draft_id, d.user_id, u.display_name, u.role_code::text AS role_code,
           d.response_option_id, o.option_type, d.opinion_text, d.created_at
      FROM member_drafts d
      JOIN users u ON u.company_id = d.company_id AND u.user_id = d.user_id
      LEFT JOIN response_options o
        ON o.company_id = d.company_id AND o.response_option_id = d.response_option_id
     WHERE d.company_id = :company_id AND d.analysis_run_id = :run_id
     ORDER BY d.created_at, d.member_draft_id
    """
).bindparams(bindparam("company_id", type_=_UUID), bindparam("run_id", type_=_UUID))

_FINAL_DECISION = text(
    """
    SELECT f.final_decision_id, f.user_id, u.display_name, u.role_code::text AS role_code,
           f.response_option_id, o.option_type, f.reason_text, f.decided_at, f.checked_seq
      FROM final_decisions f
      JOIN users u ON u.company_id = f.company_id AND u.user_id = f.user_id
      JOIN response_options o
        ON o.company_id = f.company_id AND o.response_option_id = f.response_option_id
     WHERE f.company_id = :company_id AND f.analysis_run_id = :run_id
    """
).bindparams(bindparam("company_id", type_=_UUID), bindparam("run_id", type_=_UUID))


@dataclass(frozen=True)
class AnalysisRunRow:
    analysis_run_id: uuid.UUID
    run_code: str | None
    delay_event_id: uuid.UUID
    version_no: int
    parent_analysis_run_id: uuid.UUID | None
    is_latest: bool
    status: str
    completed_status: str | None
    result_class: str | None
    status_reason: str | None
    trigger_type: str
    trigger_reason: str
    requested_by_user_id: uuid.UUID | None
    requested_by_display_name: str | None
    requested_by_role_code: str | None
    request_reason: str | None
    requested_at: datetime | None
    started_at: datetime
    finished_at: datetime | None
    stale_detected_at: datetime | None
    stale_primary_reason: str | None
    stale_reason_codes: list[str] | None
    input_seq: int
    base_date: date | None
    operating_rule_set_id: uuid.UUID
    operating_rule_version_no: int
    calculation_contract_version: str
    allocation_rule_version: str
    final_explanation_json: dict[str, Any] | None
    explanation_validation_status: str | None
    explanation_generated_at: datetime | None
    model_name: str | None
    prompt_version: str | None
    failed_step: str | None
    failure_code: str | None
    attempt_count: int | None
    retry_exhausted: bool | None
    failure_message: str | None


@dataclass(frozen=True)
class DataVersionRow:
    data_sync_run_id: uuid.UUID
    source_domain: str
    finished_at: datetime | None


@dataclass(frozen=True)
class InventoryImpactRow:
    sku_id: uuid.UUID
    sku_code: str
    product_name: str
    warehouse_id: uuid.UUID
    warehouse_code: str
    region_code: str
    sku_scope: str
    calculation_status: str
    forecast_method: str
    available_quantity: int
    committed_open_quantity: int | None
    sales_basis_type: str
    sales_basis_days: int
    sales_basis_start_date: date | None
    sales_basis_end_date: date | None
    sales_basis_event_days: int | None
    sales_basis_event_names: list[str] | None
    average_daily_sales: Decimal | None
    safety_stock_days: int
    safety_stock_quantity: int | None
    baseline_planned_3pl_inbound_date: date
    expected_3pl_inbound_date: date
    baseline_safety_stock_breach_date: date | None
    baseline_stockout_date: date | None
    baseline_stockout_days: int | None
    baseline_shortage_quantity: int | None
    safety_stock_breach_date: date | None
    stockout_date: date | None
    stockout_days: int | None
    shortage_quantity: int | None


@dataclass(frozen=True)
class AtRiskLineRow:
    at_risk_order_id: uuid.UUID
    order_id: uuid.UUID
    order_code: str
    order_line_id: uuid.UUID
    line_no: int
    sku_id: uuid.UUID
    sku_code: str
    product_name: str
    open_quantity: int
    wait_cause: str | None
    risk_attribution: str
    promised_delivery_at: datetime
    baseline_expected_delivery_at: datetime | None
    expected_delivery_at: datetime
    expected_beyond_horizon: bool
    risk_reason: str


@dataclass(frozen=True)
class ResponseOptionRow:
    response_option_id: uuid.UUID
    option_type: str
    feasibility_status: str
    reason_code: str | None
    required_quantity: int
    securable_quantity: int | None
    reduction_committed_total: int | None
    expected_delivery_date: date | None
    fully_recovered_orders: int | None
    delay_induced_recovered_orders: int | None
    item_secured_blocked_orders: int | None
    not_recovered_orders: int | None
    recommendation_rank: int | None
    is_recommended: bool
    sku_results: dict[str, Any]


@dataclass(frozen=True)
class OptionOutcomeRow:
    response_option_id: uuid.UUID
    at_risk_order_id: uuid.UUID
    outcome: str
    not_recovered_reason: str | None
    line_securement_status: str
    secured_quantity: int
    recovered_quantity: int
    expected_delivery_at: datetime | None
    source_warehouse_id: uuid.UUID | None
    source_warehouse_code: str | None
    source_region_code: str | None
    supply_source: str | None


@dataclass(frozen=True)
class MemberDraftRow:
    member_draft_id: uuid.UUID
    user_id: uuid.UUID
    display_name: str
    role_code: str
    response_option_id: uuid.UUID | None
    option_type: str | None
    opinion_text: str | None
    created_at: datetime


@dataclass(frozen=True)
class FinalDecisionRow:
    final_decision_id: uuid.UUID
    user_id: uuid.UUID
    display_name: str
    role_code: str
    response_option_id: uuid.UUID
    option_type: str
    reason_text: str
    decided_at: datetime
    checked_seq: int


class AnalysisRunDetailRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # Read
    async def find_run(
        self, company_id: uuid.UUID, run_id: uuid.UUID
    ) -> AnalysisRunRow | None:
        """고객사의 실행 1건. 없거나 다른 고객사 실행이면 None"""
        row = (
            await self.db.execute(_RUN, {"company_id": company_id, "run_id": run_id})
        ).one_or_none()
        return AnalysisRunRow(**row._mapping) if row is not None else None

    async def find_data_versions(
        self, company_id: uuid.UUID, run_id: uuid.UUID
    ) -> list[DataVersionRow]:
        return await self._rows(_DATA_VERSIONS, DataVersionRow, company_id, run_id)

    async def find_inventory_impacts(
        self, company_id: uuid.UUID, run_id: uuid.UUID
    ) -> list[InventoryImpactRow]:
        return await self._rows(_IMPACTS, InventoryImpactRow, company_id, run_id)

    async def find_at_risk_lines(
        self, company_id: uuid.UUID, run_id: uuid.UUID
    ) -> list[AtRiskLineRow]:
        return await self._rows(_AT_RISK_LINES, AtRiskLineRow, company_id, run_id)

    async def find_response_options(
        self, company_id: uuid.UUID, run_id: uuid.UUID
    ) -> list[ResponseOptionRow]:
        return await self._rows(_OPTIONS, ResponseOptionRow, company_id, run_id)

    async def find_option_outcomes(
        self, company_id: uuid.UUID, run_id: uuid.UUID
    ) -> list[OptionOutcomeRow]:
        return await self._rows(_OUTCOMES, OptionOutcomeRow, company_id, run_id)

    async def find_member_drafts(
        self, company_id: uuid.UUID, run_id: uuid.UUID
    ) -> list[MemberDraftRow]:
        return await self._rows(_DRAFTS, MemberDraftRow, company_id, run_id)

    async def find_final_decision(
        self, company_id: uuid.UUID, run_id: uuid.UUID
    ) -> FinalDecisionRow | None:
        """실행당 최종 선택은 많아야 1건입니다(final_decisions 실행 고유)."""
        rows = await self._rows(_FINAL_DECISION, FinalDecisionRow, company_id, run_id)
        return rows[0] if rows else None

    async def _rows(self, statement, row_type, company_id, run_id) -> list:
        result = await self.db.execute(
            statement, {"company_id": company_id, "run_id": run_id}
        )
        return [row_type(**row._mapping) for row in result]
