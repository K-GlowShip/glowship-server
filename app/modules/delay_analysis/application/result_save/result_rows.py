import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.modules.delay_analysis.application.agent.tools.impact_schemas import (
    AtRiskOrders,
    InventoryImpact,
)
from app.modules.delay_analysis.application.agent.tools.lookup_schemas import (
    DelayContext,
    InventorySnapshot,
    OpenOrders,
    OperatingRules,
    SalesHistory,
)
from app.modules.delay_analysis.application.agent.tools.option_schemas import (
    OptionOutcomes,
    OptionRanking,
)
from app.modules.delay_analysis.infrastructure.analysis_enums import (
    CompletedStatus,
    ResultClass,
)
from app.modules.order.infrastructure.order_enums import OrderLineStatus

"""
    분석 결과 → 저장 행 (GLOW-128 결과 저장, docs/analysis/result-save.md)
      - 에이전트가 돌려준 도구 출력(T6·T7·T10·T11)과 실행 기준값을 받아 결과 표 4개의 행과
        analysis_runs 에서 바꿀 열을 만든다. DB 를 읽지 않는 순수 함수다(저장은 result_save_service).
      - 열 규칙은 데이터 팀 엔진 storage.py rows_for(61~134행)와 같다. 열마다 근거 행을 적었다.
      - 계약 Q11("T6·T7 출력에 없는 저장 열은 결과 저장에서 더한다"): 가용·확정 수요·평균 판매는
        run_basis_from_tools 가 T2·T4·T5 출력에서 엔진 정의 그대로 계산한다.
"""

FORECAST_METHOD = "ORDER_FIRST_APPROX_V1"  # storage.py 93행
CALCULATION_STATUS = "CALCULATED"  # storage.py 93행
# NO_RISK 의 status_reason (엔진 하네스 run_s1_s5.py 167행)
NO_RISK_REASON = {
    ResultClass.WARNING_NO_RISK: "NO_DELAY_INCREMENT; PRE_EXISTING_SHORTAGE",
    ResultClass.PURE_NO_RISK: "NO_DELAY_INCREMENT; NONE",
}


class ResultSaveInputError(ValueError):
    """도구 출력끼리 맞지 않아 저장할 수 없음(계약 위반). 저장하지 않는다."""


@dataclass(frozen=True)
class SkuSupplement:
    """T6 출력에 없는 SKU별 저장 값 (계약 Q11)"""

    available_quantity: int  # D−1 도착 창고 보유 − 예약 (storage.py 88행)
    committed_open_quantity: (
        int  # 도착 창고 D−1 열린 주문의 활성 상세 주문 수량 합 (core.forecast_sku 25행)
    )
    average_daily_sales: (
        Decimal  # 도착 창고 28일 수요 ÷ 28, 소수 6자리 (storage.py 95행)
    )


@dataclass(frozen=True)
class RunBasis:
    """같은 실행의 다른 도구 출력에서 옮겨 오는 값 (T1·T4·T8 + Q11)"""

    destination_warehouse_id: uuid.UUID
    planned_3pl_inbound_date: date  # P
    expected_3pl_inbound_date: date  # E
    sales_basis_type: str
    sales_basis_days: int
    sales_basis_start_date: date
    sales_basis_end_date: date
    sales_basis_event_days: int
    sales_basis_event_names: list[str]
    safety_stock_days: int
    skus: Mapping[str, SkuSupplement]  # sku_code → 값


@dataclass(frozen=True)
class Explanation:
    """검증을 통과한 AI 설명 (GLOW-127). 없으면 설명 열을 비운 채 저장한다."""

    model_name: str
    prompt_version: str
    final_explanation_json: dict[str, Any]
    generated_at: datetime


@dataclass(frozen=True)
class AnalysisResult:
    """에이전트가 돌려주는 정상 완료 결과 (agentflow 5.5 "정상"·"위험 주문 없음" 행)"""

    basis: RunBasis
    inventory_impact: InventoryImpact  # T6
    at_risk_orders: AtRiskOrders  # T7
    option_outcomes: OptionOutcomes | None  # T10 (READY_FOR_REVIEW 만)
    option_ranking: OptionRanking | None  # T11 (READY_FOR_REVIEW 만)
    dependency_scope: dict[str, Any]
    decision_signature: dict[str, Any]
    decision_signature_sha256: str
    explanation: Explanation | None = None


@dataclass
class ResultRows:
    """저장할 행. 키는 ORM 열 이름과 같다(company_id·analysis_run_id 포함)."""

    run_values: dict[str, Any]
    inventory_impact_results: list[dict[str, Any]] = field(default_factory=list)
    at_risk_orders: list[dict[str, Any]] = field(default_factory=list)
    response_options: list[dict[str, Any]] = field(default_factory=list)
    option_order_outcomes: list[dict[str, Any]] = field(default_factory=list)


def _demand_average(total: int) -> Decimal:
    """엔진은 float 평균을 round(·, 6) 한 뒤 repr 로 저장한다 (storage.py 95행). 같은 값을 만든다."""
    return Decimal(repr(round(total / 28, 6)))


def run_basis_from_tools(
    delay_context: DelayContext,
    inventory_snapshot: InventorySnapshot,
    sales_history: SalesHistory,
    open_orders: OpenOrders,
    operating_rules: OperatingRules,
    sku_codes: list[str],
) -> RunBasis:
    """T1·T2·T4·T5·T8 출력에서 저장 기준값을 만든다. sku_codes 는 T6 items 의 SKU 코드."""
    dest = delay_context.destination_warehouse
    stock = {
        item.sku_code: item
        for wh in inventory_snapshot.warehouses
        if wh.warehouse_code == dest.warehouse_code
        for item in wh.items
    }
    demand: dict[str, int] = {}
    for (
        d
    ) in sales_history.demand_totals:  # 채널별 합 = 전체 수요 (엔진 store.py 133·134행)
        if d.warehouse_code == dest.warehouse_code:
            demand[d.sku_code] = demand.get(d.sku_code, 0) + d.quantity
    committed: dict[str, int] = {}
    for order in open_orders.orders:  # T5 는 도착·공여 창고 주문이 함께 온다
        if order.warehouse_id != dest.warehouse_id:
            continue
        for line in order.lines:  # 활성 상세만 (core.to_alloc_order_lines 54행)
            if line.line_status == OrderLineStatus.ACTIVE:
                committed[line.sku_code] = (
                    committed.get(line.sku_code, 0) + line.ordered_quantity
                )
    skus = {}
    for code in sku_codes:
        item = stock.get(code)
        on_hand = item.on_hand_quantity if item else 0  # 엔진 .get(sku, 0)
        reserved = item.reserved_quantity if item else 0
        skus[code] = SkuSupplement(
            available_quantity=on_hand - reserved,
            committed_open_quantity=committed.get(code, 0),
            average_daily_sales=_demand_average(demand.get(code, 0)),
        )
    return RunBasis(
        destination_warehouse_id=uuid.UUID(dest.warehouse_id),
        planned_3pl_inbound_date=delay_context.planned_3pl_inbound_date,
        expected_3pl_inbound_date=delay_context.expected_3pl_inbound_date,
        sales_basis_type=sales_history.sales_basis_type,
        sales_basis_days=operating_rules.sales_basis_days,  # 엔진 snap["basis_days"] = 운영 기준 값
        sales_basis_start_date=sales_history.basis_start_date,
        sales_basis_end_date=sales_history.basis_end_date,
        sales_basis_event_days=sales_history.event_days,
        sales_basis_event_names=list(sales_history.event_names),
        safety_stock_days=operating_rules.safety_stock_days,
        skus=skus,
    )


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value)  # 계약 형식 YYYY-MM-DDTHH:MM:SSZ (UTC)


def _end_of_day(day: date) -> datetime:
    return datetime.fromisoformat(
        f"{day.isoformat()}T23:59:59Z"
    )  # storage.py 109·110행


def _check(result: AnalysisResult) -> None:
    impact = result.inventory_impact
    status = CompletedStatus(impact.completed_status)
    has_options = bool(result.option_outcomes and result.option_outcomes.options)
    if status == CompletedStatus.READY_FOR_REVIEW:
        if not has_options or result.option_ranking is None:
            raise ResultSaveInputError(
                "READY_FOR_REVIEW 는 대응안(T10)과 순위(T11)가 있어야 합니다"
            )
        outcome_types = [o.option_type for o in result.option_outcomes.options]
        rank_types = [o.option_type for o in result.option_ranking.options]
        if outcome_types != rank_types:
            raise ResultSaveInputError(
                f"T10·T11 대응안이 다릅니다: {outcome_types} / {rank_types}"
            )
    elif has_options or result.option_ranking is not None:
        # 엔진은 READY_FOR_REVIEW 일 때만 대응안을 계산한다 (pipeline.analyze 75행)
        raise ResultSaveInputError("NO_RISK 에는 대응안이 없어야 합니다")
    missing = [i.sku_code for i in impact.items if i.sku_code not in result.basis.skus]
    if missing:
        raise ResultSaveInputError(f"저장 기준값(Q11)이 없는 SKU: {missing}")
    if result.dependency_scope.get("schema_version") != "dependency_scope.v1":
        raise ResultSaveInputError(
            "의존 범위는 dependency_scope.v1 이어야 합니다 (ck38)"
        )
    if result.decision_signature.get("schema_version") != "decision_signature.v1":
        raise ResultSaveInputError(
            "판정 서명은 decision_signature.v1 이어야 합니다 (ck39)"
        )


def build_result_rows(
    result: AnalysisResult, *, company_id: uuid.UUID, analysis_run_id: uuid.UUID
) -> ResultRows:
    """정상 완료 결과를 저장 행으로 바꾼다. 도구 출력끼리 맞지 않으면 ResultSaveInputError."""
    _check(result)
    impact, basis = result.inventory_impact, result.basis
    status = CompletedStatus(impact.completed_status)
    result_class = ResultClass(impact.result_class)
    common = {"company_id": company_id, "analysis_run_id": analysis_run_id}

    run_values: dict[str, Any] = {
        "status": status.value,
        "completed_status": status.value,
        "result_class": result_class.value,
        "status_reason": NO_RISK_REASON.get(result_class)
        if status == CompletedStatus.NO_RISK
        else None,
        "dependency_scope": result.dependency_scope,
        "decision_signature": result.decision_signature,
        "decision_signature_sha256": result.decision_signature_sha256,
    }
    if result.explanation is not None:
        run_values.update(
            model_name=result.explanation.model_name,
            prompt_version=result.explanation.prompt_version,
            final_explanation_json=result.explanation.final_explanation_json,
            explanation_validation_status="PASSED",
            explanation_generated_at=result.explanation.generated_at,
        )
    rows = ResultRows(run_values=run_values)

    for item in impact.items:  # storage.py 86~101행
        sku = basis.skus[item.sku_code]
        rows.inventory_impact_results.append(
            {
                **common,
                "inventory_impact_result_id": uuid.uuid4(),
                "sku_id": uuid.UUID(item.sku_id),
                "warehouse_id": basis.destination_warehouse_id,
                "sku_scope": item.sku_scope,
                "calculation_status": CALCULATION_STATUS,
                "forecast_method": FORECAST_METHOD,
                "available_quantity": sku.available_quantity,
                "committed_open_quantity": sku.committed_open_quantity,
                "sales_basis_type": basis.sales_basis_type,
                "sales_basis_days": basis.sales_basis_days,
                "sales_basis_start_date": basis.sales_basis_start_date,
                "sales_basis_end_date": basis.sales_basis_end_date,
                "sales_basis_event_days": basis.sales_basis_event_days,
                "sales_basis_event_names": basis.sales_basis_event_names
                or None,  # 95행: 없으면 NULL
                "average_daily_sales": sku.average_daily_sales,
                "safety_stock_days": basis.safety_stock_days,
                "safety_stock_quantity": item.safety_stock_quantity,
                "baseline_planned_3pl_inbound_date": basis.planned_3pl_inbound_date,
                "expected_3pl_inbound_date": basis.expected_3pl_inbound_date,
                "raw_baseline_shortage_quantity": Decimal(item.raw_baseline_shortage),
                "raw_shortage_quantity": Decimal(item.raw_total_shortage),
                "raw_increment_shortage_quantity": Decimal(item.raw_increment_shortage),
                "baseline_safety_stock_breach_date": item.baseline.safety_stock_breach_date,
                "baseline_stockout_date": item.baseline.stockout_date,
                "baseline_stockout_days": item.baseline.stockout_days,
                "baseline_shortage_quantity": item.baseline_prepare_quantity,
                "safety_stock_breach_date": item.total.safety_stock_breach_date,
                "stockout_date": item.total.stockout_date,
                "stockout_days": item.total.stockout_days,
                "shortage_quantity": item.total_prepare_quantity,
            }
        )

    risk_id: dict[str, uuid.UUID] = {}
    for risk in result.at_risk_orders.risks:  # storage.py 103~111행 (상세 행 단위)
        promised = _timestamp(risk.promised_delivery_at)
        reason = (
            f"{risk.risk_attribution} 예상 배송 {risk.expected_delivery_date.isoformat()}"
            f" > 약속 {promised.date().isoformat()}"
        )
        for line in risk.lines:
            row_id = uuid.uuid4()
            risk_id[line.order_line_id] = row_id
            rows.at_risk_orders.append(
                {
                    **common,
                    "at_risk_order_id": row_id,
                    "order_line_id": uuid.UUID(line.order_line_id),
                    "open_quantity": line.open_quantity,
                    "promised_delivery_at": promised,
                    "expected_delivery_at": _end_of_day(risk.expected_delivery_date),
                    "baseline_expected_delivery_at": _end_of_day(
                        risk.baseline_expected_delivery_date
                    ),
                    "expected_beyond_horizon": risk.expected_beyond_horizon,
                    "risk_attribution": risk.risk_attribution,
                    "wait_cause": line.wait_cause,
                    "risk_reason": reason,
                }
            )

    if status != CompletedStatus.READY_FOR_REVIEW:
        return rows
    ranks = {r.option_type: r for r in result.option_ranking.options}
    for option in result.option_outcomes.options:  # storage.py 112~134행
        option_id = uuid.uuid4()
        rank = ranks[option.option_type]
        rows.response_options.append(
            {
                **common,
                "response_option_id": option_id,
                "option_type": option.option_type,
                "feasibility_status": option.feasibility_status,
                "reason_code": option.reason_code,
                # 118행 "FEASIBLE 이면 NULL, 아니면 사유 코드" = 사유 코드 그대로 (ck5: FEASIBLE ⇔ reason_code NULL)
                "reason": option.reason_code,
                "required_quantity": option.required_quantity,
                "securable_quantity": option.securable_quantity,
                "reduction_committed_total": option.reduction_committed_total,
                "expected_delivery_date": option.expected_delivery_date,
                "fully_recovered_orders": option.fully_recovered_orders,
                "delay_induced_recovered_orders": option.delay_induced_recovered_orders,
                "item_secured_blocked_orders": option.item_secured_blocked_orders,
                "not_recovered_orders": option.not_recovered_orders,
                "recommendation_rank": rank.recommendation_rank,
                "is_recommended": rank.is_recommended,
                "sku_results": option.sku_results.model_dump(mode="json"),
            }
        )
        for outcome in option.outcomes:
            at_risk_id = risk_id.get(outcome.order_line_id)
            if at_risk_id is None:
                raise ResultSaveInputError(
                    f"{option.option_type} 결과의 상세 {outcome.order_line_id} 가 위험 주문(T7)에 없습니다"
                )
            rows.option_order_outcomes.append(
                {
                    **common,
                    "option_order_outcome_id": uuid.uuid4(),
                    "response_option_id": option_id,
                    "at_risk_order_id": at_risk_id,
                    "outcome": outcome.outcome,
                    "not_recovered_reason": outcome.not_recovered_reason,
                    "expected_delivery_at": _timestamp(outcome.expected_delivery_at)
                    if outcome.expected_delivery_at
                    else None,
                    "line_securement_status": outcome.line_securement_status,
                    "secured_quantity": outcome.secured_quantity,
                    "recovered_quantity": outcome.recovered_quantity,
                    "source_warehouse_id": uuid.UUID(outcome.source_warehouse_id)
                    if outcome.source_warehouse_id
                    else None,
                    "supply_source": outcome.supply_source,
                }
            )
    return rows
