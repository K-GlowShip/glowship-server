import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.modules.delay_analysis.application.agent.tools import (
    impact_schemas as I,
)
from app.modules.delay_analysis.application.agent.tools import (
    lookup_schemas as L,
)
from app.modules.delay_analysis.application.agent.tools import (
    option_schemas as OS,
)
from app.modules.delay_analysis.application.result_save.result_rows import (
    AnalysisResult,
    Explanation,
    ResultSaveInputError,
    RunBasis,
    SkuSupplement,
    build_result_rows,
    run_basis_from_tools,
)
from app.modules.delay_analysis.application.result_save.result_save_service import (
    save_analysis_result,
)
from app.modules.delay_analysis.infrastructure.analysis_run import AnalysisRun
from app.modules.delay_analysis.infrastructure.at_risk_order import AtRiskOrder
from app.modules.delay_analysis.infrastructure.inventory_impact_result import (
    InventoryImpactResult,
)
from app.modules.delay_analysis.infrastructure.option_order_outcome import (
    OptionOrderOutcome,
)
from app.modules.delay_analysis.infrastructure.response_option import ResponseOption
from app.modules.order.infrastructure.order_enums import OrderLineStatus

"""
    결과 저장 시험 (GLOW-128, docs/analysis/result-save.md)
      1. 저장 기준값(Q11): T2·T4·T5 출력에서 엔진 정의대로 계산하는지
      2. 행 만들기: 도구 출력끼리 맞지 않으면 거부하는지, 엔진 규칙(status_reason·reason)
      3. overlay 왕복: DB 의 정상 완료 실행마다 결과 행을 도구 출력으로 되돌려 → 결과 행을 지우고 실행을
         진행 중으로 되돌린 뒤 → 저장 → 원래 행과 열 단위로 같은지(ID 제외). 모두 롤백된다.
      4. STALE: 저장 직전에 STALE 이면 아무것도 쓰지 않는다
"""

ID = "11111111-2222-3333-4444-555555555555"
DEST = "aaaaaaaa-0000-0000-0000-000000000001"
DONOR = "aaaaaaaa-0000-0000-0000-000000000002"


# --- 1. 저장 기준값(Q11) --------------------------------------------------------------------------


def _line(sku, qty, status=OrderLineStatus.ACTIVE):
    return L.OpenOrderLine.model_construct(
        sku_code=sku, ordered_quantity=qty, line_status=status
    )


def test_run_basis_matches_engine_definitions():
    dest = L.WarehouseRef.model_construct(
        warehouse_id=DEST, warehouse_code="LAX", region_code="WEST"
    )
    basis = run_basis_from_tools(
        L.DelayContext.model_construct(
            destination_warehouse=dest,
            planned_3pl_inbound_date=date(2026, 1, 10),
            expected_3pl_inbound_date=date(2026, 1, 20),
        ),
        L.InventorySnapshot.model_construct(
            warehouses=[
                L.WarehouseInventory.model_construct(
                    warehouse_code="LAX",
                    items=[
                        L.InventoryItem.model_construct(
                            sku_code="A", on_hand_quantity=50, reserved_quantity=8
                        )
                    ],
                ),
                L.WarehouseInventory.model_construct(  # 공여 창고 재고는 쓰지 않음
                    warehouse_code="EWR",
                    items=[
                        L.InventoryItem.model_construct(
                            sku_code="A", on_hand_quantity=999, reserved_quantity=0
                        )
                    ],
                ),
            ]
        ),
        L.SalesHistory.model_construct(
            sales_basis_type="ORDER_DEMAND",
            basis_start_date=date(2025, 12, 13),
            basis_end_date=date(2026, 1, 9),
            demand_totals=[  # 채널별 합 = 전체 수요
                L.DemandTotal.model_construct(
                    warehouse_code="LAX",
                    sku_code="A",
                    sales_channel="AMAZON",
                    quantity=30,
                ),
                L.DemandTotal.model_construct(
                    warehouse_code="LAX",
                    sku_code="A",
                    sales_channel="SEPHORA",
                    quantity=11,
                ),
                L.DemandTotal.model_construct(
                    warehouse_code="EWR",
                    sku_code="A",
                    sales_channel="AMAZON",
                    quantity=500,
                ),
            ],
            event_days=0,
            event_names=[],
        ),
        L.OpenOrders.model_construct(
            orders=[
                L.OpenOrder.model_construct(
                    warehouse_id=DEST,
                    lines=[
                        _line("A", 3),
                        _line("A", 4, OrderLineStatus.REMOVED_AT_SOURCE),
                        _line("B", 2),
                    ],
                ),
                L.OpenOrder.model_construct(
                    warehouse_id=DONOR, lines=[_line("A", 100)]
                ),  # 공여 창고 주문은 빼고
            ]
        ),
        L.OperatingRules.model_construct(sales_basis_days=28, safety_stock_days=7),
        ["A", "B", "C"],
    )
    assert basis.destination_warehouse_id == uuid.UUID(DEST)
    assert basis.skus["A"] == SkuSupplement(
        42, 3, Decimal("1.464286")
    )  # 41 / 28 = 1.4642857…
    assert basis.skus["B"] == SkuSupplement(
        0, 2, Decimal("0.0")
    )  # 재고 행 없음 → 0 (엔진 .get)
    assert basis.skus["C"].average_daily_sales == Decimal(repr(round(0 / 28, 6)))
    assert (basis.sales_basis_days, basis.safety_stock_days) == (28, 7)


# --- 2. 행 만들기 ---------------------------------------------------------------------------------


def _basis(codes=("SKU-1",)):
    return RunBasis(
        destination_warehouse_id=uuid.UUID(DEST),
        planned_3pl_inbound_date=date(2026, 1, 10),
        expected_3pl_inbound_date=date(2026, 1, 20),
        sales_basis_type="ORDER_DEMAND",
        sales_basis_days=28,
        sales_basis_start_date=date(2025, 12, 13),
        sales_basis_end_date=date(2026, 1, 9),
        sales_basis_event_days=0,
        sales_basis_event_names=[],
        safety_stock_days=7,
        skus={c: SkuSupplement(5, 1, Decimal("1.5")) for c in codes},
    )


def _path():
    return I.ForecastPath(
        stockout_date=None,
        stockout_days=0,
        safety_stock_breach_date=None,
        min_gap=None,
        daily_deficits=[],
    )


def _impact(status="NO_RISK", result_class="PURE_NO_RISK"):
    item = I.InventoryImpactItem(
        sku_id=ID, sku_code="SKU-1", sku_scope="SHIPMENT_ONLY", raw_baseline_shortage="0.0000",
        raw_total_shortage="0.0000", raw_increment_shortage="0.0000", baseline_prepare_quantity=0,
        increment_prepare_quantity=0, total_prepare_quantity=0, safety_stock_quantity=3,
        baseline=_path(), total=_path(),
    )  # fmt: skip
    return I.InventoryImpact(
        items=[item],
        impacted_sku_codes=[],
        watch_sku_codes=[],
        completed_status=status,
        result_class=result_class,
    )


def _no_risks():
    return I.AtRiskOrders(
        risks=[],
        delay_induced_sku_codes=[],
        simulation_horizon_end="2026-02-05",
        expected_plan_simulation={},
    )


def _result(**overrides):
    body = {
        "basis": _basis(),
        "inventory_impact": _impact(),
        "at_risk_orders": _no_risks(),
        "option_outcomes": None,
        "option_ranking": None,
        "dependency_scope": {"schema_version": "dependency_scope.v1", "sku_sets": {}},
        "decision_signature": {
            "schema_version": "decision_signature.v1",
            "options": [],
        },
        "decision_signature_sha256": "0" * 64,
    }
    body.update(overrides)
    return AnalysisResult(**body)


def test_no_risk_rows_follow_engine():
    run_id, company = uuid.uuid4(), uuid.uuid4()
    rows = build_result_rows(_result(), company_id=company, analysis_run_id=run_id)
    assert rows.run_values["status_reason"] == "NO_DELAY_INCREMENT; NONE"
    assert rows.run_values["status"] == rows.run_values["completed_status"] == "NO_RISK"
    assert (
        "final_explanation_json" not in rows.run_values
    )  # 설명이 없으면 설명 열은 건드리지 않음
    (row,) = rows.inventory_impact_results
    assert (row["company_id"], row["analysis_run_id"], row["warehouse_id"]) == (
        company,
        run_id,
        uuid.UUID(DEST),
    )
    assert row["sales_basis_event_names"] is None  # 빈 목록 → NULL (storage.py 95행)
    assert (row["calculation_status"], row["forecast_method"]) == (
        "CALCULATED",
        "ORDER_FIRST_APPROX_V1",
    )
    assert rows.response_options == [] and rows.option_order_outcomes == []
    warning = build_result_rows(
        _result(inventory_impact=_impact("NO_RISK", "WARNING_NO_RISK")),
        company_id=company,
        analysis_run_id=run_id,
    )
    assert (
        warning.run_values["status_reason"]
        == "NO_DELAY_INCREMENT; PRE_EXISTING_SHORTAGE"
    )


def test_inconsistent_tool_outputs_are_rejected():
    ready = _impact("READY_FOR_REVIEW", "FORECAST_ONLY_READY")
    bad = {
        "READY 인데 대응안 없음": _result(inventory_impact=ready),
        "NO_RISK 인데 순위 있음": _result(
            option_ranking=OS.OptionRanking.model_construct(options=[])
        ),
        "Q11 값 없는 SKU": _result(basis=_basis(codes=("OTHER",))),
        "의존 범위 버전": _result(dependency_scope={"schema_version": "x"}),
        "판정 서명 버전": _result(decision_signature={"schema_version": "x"}),
    }
    for label, result in bad.items():
        with pytest.raises(ResultSaveInputError):
            build_result_rows(
                result, company_id=uuid.uuid4(), analysis_run_id=uuid.uuid4()
            )
        assert label


# --- 3·4. overlay 왕복과 STALE (실제 DB, 롤백) ------------------------------------------------------

_RESULT_RUNS = text(
    """
    SELECT r.analysis_run_id, r.company_id
      FROM analysis_runs r
     WHERE r.completed_status IN ('READY_FOR_REVIEW', 'NO_RISK')
       AND EXISTS (SELECT 1 FROM inventory_impact_results i WHERE i.analysis_run_id = r.analysis_run_id)
     ORDER BY r.analysis_run_id
    """
)
_CODES = text(
    """
    SELECT a.order_line_id, ol.line_no, o.order_id, o.order_code, o.sales_channel, o.ordered_at, s.sku_code
      FROM at_risk_orders a
      JOIN order_lines ol ON ol.order_line_id = a.order_line_id
      JOIN orders o ON o.order_id = ol.order_id
      JOIN skus s ON s.sku_id = ol.sku_id
     WHERE a.analysis_run_id = :run_id
    """
)
_SKU_CODES = text("SELECT sku_id, sku_code FROM skus WHERE sku_id = ANY(:ids)")
_CLEAR = [
    "DELETE FROM final_decisions WHERE response_option_id IN (SELECT response_option_id FROM response_options WHERE analysis_run_id = :run_id)",
    "DELETE FROM member_drafts WHERE analysis_run_id = :run_id",
    "DELETE FROM option_order_outcomes WHERE analysis_run_id = :run_id",
    "DELETE FROM response_options WHERE analysis_run_id = :run_id",
    "DELETE FROM at_risk_orders WHERE analysis_run_id = :run_id",
    "DELETE FROM inventory_impact_results WHERE analysis_run_id = :run_id",
    """UPDATE analysis_runs SET status = 'ANALYZING_OPTIONS', completed_status = NULL, result_class = NULL,
              status_reason = NULL, dependency_scope = NULL, decision_signature = NULL, decision_signature_sha256 = NULL,
              stale_primary_reason = NULL, stale_reason_codes = NULL, stale_detected_seq = NULL, stale_detected_at = NULL,
              finished_at = NULL
        WHERE analysis_run_id = :run_id""",
]
_RUN_COLUMNS = ("completed_status", "result_class", "status_reason", "dependency_scope", "decision_signature", "decision_signature_sha256",
                "model_name", "prompt_version", "final_explanation_json", "explanation_validation_status", "explanation_generated_at")  # fmt: skip


def _z(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _columns(obj, drop=()):
    return {
        c.key: getattr(obj, c.key) for c in obj.__table__.columns if c.key not in drop
    }


async def _rows(db, entity, run_id):
    stmt = (
        select(entity)
        .where(entity.analysis_run_id == run_id)
        .execution_options(populate_existing=True)
    )
    return (await db.scalars(stmt)).all()


async def _snapshot(db, run_id):
    """ID 를 빼고 자연 키로 묶은 결과 행 (비교용)"""
    risks = await _rows(db, AtRiskOrder, run_id)
    options = await _rows(db, ResponseOption, run_id)
    line_of = {r.at_risk_order_id: r.order_line_id for r in risks}
    type_of = {o.response_option_id: o.option_type for o in options}
    run = await db.scalar(
        select(AnalysisRun)
        .where(AnalysisRun.analysis_run_id == run_id)
        .execution_options(populate_existing=True)
    )
    return {
        "run": {k: getattr(run, k) for k in _RUN_COLUMNS},
        "inventory": {r.sku_id: _columns(r, {"inventory_impact_result_id"}) for r in await _rows(db, InventoryImpactResult, run_id)},
        "risks": {r.order_line_id: _columns(r, {"at_risk_order_id"}) for r in risks},
        "options": {o.option_type: _columns(o, {"response_option_id"}) for o in options},
        "outcomes": {
            (type_of[x.response_option_id], line_of[x.at_risk_order_id]): _columns(
                x, {"option_order_outcome_id", "response_option_id", "at_risk_order_id"}
            )
            for x in await _rows(db, OptionOrderOutcome, run_id)
        },
    }  # fmt: skip


async def _result_from_db(db, run_id) -> AnalysisResult:
    """저장된 결과 행을 에이전트가 돌려줄 도구 출력 모양으로 되돌린다. 저장하지 않는 값(min_gap·일별 부족·시뮬레이션)은 빈 값."""
    run = await db.scalar(
        select(AnalysisRun).where(AnalysisRun.analysis_run_id == run_id)
    )
    inv = await _rows(db, InventoryImpactResult, run_id)
    codes = dict((await db.execute(_SKU_CODES, {"ids": [r.sku_id for r in inv]})).all())
    first = inv[0]

    def path(stockout_date, stockout_days, breach):
        return I.ForecastPath(
            stockout_date=stockout_date,
            stockout_days=stockout_days,
            safety_stock_breach_date=breach,
            min_gap=None,
            daily_deficits=[],
        )

    items = [
        I.InventoryImpactItem(
            sku_id=str(r.sku_id), sku_code=codes[r.sku_id], sku_scope=r.sku_scope,
            raw_baseline_shortage=str(r.raw_baseline_shortage_quantity), raw_total_shortage=str(r.raw_shortage_quantity),
            raw_increment_shortage=str(r.raw_increment_shortage_quantity), baseline_prepare_quantity=r.baseline_shortage_quantity,
            increment_prepare_quantity=r.shortage_quantity - r.baseline_shortage_quantity, total_prepare_quantity=r.shortage_quantity,
            safety_stock_quantity=r.safety_stock_quantity,
            baseline=path(r.baseline_stockout_date, r.baseline_stockout_days, r.baseline_safety_stock_breach_date),
            total=path(r.stockout_date, r.stockout_days, r.safety_stock_breach_date),
        )
        for r in sorted(inv, key=lambda r: codes[r.sku_id])
    ]  # fmt: skip
    impact = I.InventoryImpact(
        items=items,
        impacted_sku_codes=[i.sku_code for i in items if i.sku_scope == "IMPACTED"],
        watch_sku_codes=[i.sku_code for i in items if i.sku_scope == "WATCH"],
        completed_status=run.completed_status,
        result_class=run.result_class,
    )
    basis = RunBasis(
        destination_warehouse_id=first.warehouse_id,
        planned_3pl_inbound_date=first.baseline_planned_3pl_inbound_date,
        expected_3pl_inbound_date=first.expected_3pl_inbound_date,
        sales_basis_type=first.sales_basis_type,
        sales_basis_days=first.sales_basis_days,
        sales_basis_start_date=first.sales_basis_start_date,
        sales_basis_end_date=first.sales_basis_end_date,
        sales_basis_event_days=first.sales_basis_event_days,
        sales_basis_event_names=list(first.sales_basis_event_names or []),
        safety_stock_days=first.safety_stock_days,
        skus={codes[r.sku_id]: SkuSupplement(r.available_quantity, r.committed_open_quantity, r.average_daily_sales) for r in inv},
    )  # fmt: skip

    risk_rows = await _rows(db, AtRiskOrder, run_id)
    meta = {
        m.order_line_id: m for m in (await db.execute(_CODES, {"run_id": run_id})).all()
    }
    orders: dict = {}
    for a in sorted(
        risk_rows,
        key=lambda a: (meta[a.order_line_id].order_code, meta[a.order_line_id].line_no),
    ):
        m = meta[a.order_line_id]
        order = orders.setdefault(m.order_id, {
            "order_id": str(m.order_id), "order_code": m.order_code, "sales_channel": m.sales_channel, "ordered_at": _z(m.ordered_at),
            "promised_delivery_at": _z(a.promised_delivery_at), "risk_attribution": a.risk_attribution,
            "expected_delivery_date": a.expected_delivery_at.astimezone(UTC).date(),
            "baseline_expected_delivery_date": a.baseline_expected_delivery_at.astimezone(UTC).date(),
            "expected_beyond_horizon": a.expected_beyond_horizon, "lines": [],
        })  # fmt: skip
        order["lines"].append({"order_line_id": str(a.order_line_id), "line_no": m.line_no, "sku_code": m.sku_code,
                               "open_quantity": a.open_quantity, "wait_cause": a.wait_cause})  # fmt: skip
    induced = sorted(
        {
            ln["sku_code"]
            for o in orders.values()
            if o["risk_attribution"] == "DELAY_INDUCED"
            for ln in o["lines"]
        }
    )
    at_risk = I.AtRiskOrders(
        risks=list(orders.values()),
        delay_induced_sku_codes=induced,
        simulation_horizon_end="2099-12-31",
        expected_plan_simulation={},
    )

    outcomes, ranking = None, None
    options = await _rows(db, ResponseOption, run_id)
    if options:
        line_of = {a.at_risk_order_id: str(a.order_line_id) for a in risk_rows}
        by_option: dict = {}
        for x in await _rows(db, OptionOrderOutcome, run_id):
            by_option.setdefault(x.response_option_id, []).append({
                "order_line_id": line_of[x.at_risk_order_id], "outcome": x.outcome, "not_recovered_reason": x.not_recovered_reason,
                "line_securement_status": x.line_securement_status, "secured_quantity": x.secured_quantity,
                "recovered_quantity": x.recovered_quantity,
                "expected_delivery_at": _z(x.expected_delivery_at) if x.expected_delivery_at else None,
                "source_warehouse_id": str(x.source_warehouse_id) if x.source_warehouse_id else None, "supply_source": x.supply_source,
            })  # fmt: skip
        options.sort(key=lambda o: OS.OPTION_ORDER.index(o.option_type))
        outcomes = OS.OptionOutcomes(options=[
            OS.OptionOutcome(
                option_type=o.option_type, feasibility_status=o.feasibility_status, reason_code=o.reason_code,
                required_quantity=o.required_quantity, securable_quantity=o.securable_quantity,
                reduction_committed_total=o.reduction_committed_total, expected_delivery_date=o.expected_delivery_date,
                fully_recovered_orders=o.fully_recovered_orders, delay_induced_recovered_orders=o.delay_induced_recovered_orders,
                item_secured_blocked_orders=o.item_secured_blocked_orders, not_recovered_orders=o.not_recovered_orders,
                sku_results=o.sku_results, outcomes=sorted(by_option.get(o.response_option_id, []), key=lambda x: x["order_line_id"]),
            )
            for o in options
        ])  # fmt: skip
        ranking = OS.OptionRanking(options=[
            OS.OptionRank(option_type=o.option_type, recommendation_rank=o.recommendation_rank, is_recommended=o.is_recommended) for o in options
        ])  # fmt: skip
    return AnalysisResult(
        basis=basis, inventory_impact=impact, at_risk_orders=at_risk, option_outcomes=outcomes, option_ranking=ranking,
        dependency_scope=run.dependency_scope, decision_signature=run.decision_signature,
        decision_signature_sha256=run.decision_signature_sha256,
        explanation=Explanation(run.model_name, run.prompt_version, run.final_explanation_json, run.explanation_generated_at)
        if run.final_explanation_json is not None else None,
    )  # fmt: skip


async def _clear(db, run_id):
    for sql in _CLEAR:
        await db.execute(text(sql), {"run_id": run_id})
    db.expire_all()


async def test_overlay_results_round_trip(db_write_session):
    db = db_write_session
    runs = (await db.execute(_RESULT_RUNS)).all()
    if not runs:
        pytest.skip("정상 완료 결과가 있는 실행이 없는 DB 라 건너뜀")
    counts = {"inventory": 0, "risks": 0, "options": 0, "outcomes": 0}
    for run_id, company_id in runs:
        before = await _snapshot(db, run_id)
        result = await _result_from_db(db, run_id)
        await _clear(db, run_id)
        outcome = await save_analysis_result(
            db, company_id=company_id, analysis_run_id=run_id, result=result
        )
        assert outcome.saved and outcome.status == before["run"]["completed_status"]
        after = await _snapshot(db, run_id)
        for part in ("run", *counts):
            assert after[part] == before[part], f"{run_id} {part} 이 원래 행과 다름"
        for part in counts:
            counts[part] += len(after[part])
        for entity, key in (
            (InventoryImpactResult, "inventory_impact_result_id"),
            (AtRiskOrder, "at_risk_order_id"),
            (ResponseOption, "response_option_id"),
            (OptionOrderOutcome, "option_order_outcome_id"),
        ):
            ids = [getattr(r, key) for r in await _rows(db, entity, run_id)]
            assert all(i.version == 7 for i in ids), (
                f"{key} 가 UUIDv7 이 아님"
            )  # app/core/orm.py 기본값과 같은 규칙
        finished = await db.scalar(
            select(AnalysisRun.finished_at).where(AnalysisRun.analysis_run_id == run_id)
        )
        assert finished is not None
    assert (
        len(runs) == 17
    )  # STALE 14 + FINALIZED 2 + NO_RISK 1 (설명 실패 FAILED 1건은 실패 저장 범위라 뺌)
    assert counts == {"inventory": 376, "risks": 7, "options": 36, "outcomes": 21}, (
        counts
    )  # 16시 SQL로 따로 센 값


async def test_stale_run_is_not_overwritten(db_write_session):
    db = db_write_session
    runs = (await db.execute(_RESULT_RUNS)).all()
    if not runs:
        pytest.skip("정상 완료 결과가 있는 실행이 없는 DB 라 건너뜀")
    run_id, company_id = runs[0]
    result = await _result_from_db(db, run_id)
    await _clear(db, run_id)
    await db.execute(
        text(
            """UPDATE analysis_runs SET status = 'STALE', stale_primary_reason = 'TIME_ELAPSED',
                      stale_reason_codes = ARRAY['TIME_ELAPSED'], stale_detected_at = now()
                WHERE analysis_run_id = :run_id"""
        ),
        {"run_id": run_id},
    )
    db.expire_all()
    outcome = await save_analysis_result(
        db, company_id=company_id, analysis_run_id=run_id, result=result
    )
    assert (outcome.saved, outcome.status) == (False, "STALE")
    run = await db.scalar(
        select(AnalysisRun)
        .where(AnalysisRun.analysis_run_id == run_id)
        .execution_options(populate_existing=True)
    )
    assert (run.status, run.completed_status, run.finished_at) == ("STALE", None, None)
    for entity in (
        InventoryImpactResult,
        AtRiskOrder,
        ResponseOption,
        OptionOrderOutcome,
    ):
        assert await _rows(db, entity, run_id) == [], (
            f"{entity.__tablename__} 에 행이 생김"
        )


# --- 6. 설명 필수와 완료 뒤 재판정 (#38 리뷰) -------------------------------------------------------


def test_ready_needs_explanation_and_no_risk_has_none():
    import asyncio

    ready = _impact("READY_FOR_REVIEW", "FORECAST_ONLY_READY")
    explanation = Explanation(
        "mock/m",
        "p@1",
        {"schema_version": "explanation.v1"},
        datetime(2026, 1, 1, tzinfo=UTC),
    )
    for result in (
        _result(inventory_impact=ready),
        _result(explanation=explanation),
    ):  # READY 설명 없음 / NO_RISK 설명 있음
        with pytest.raises(ResultSaveInputError):
            asyncio.run(
                save_analysis_result(
                    None,
                    company_id=uuid.uuid4(),
                    analysis_run_id=uuid.uuid4(),
                    result=result,
                )
            )


# 최신 버전이면서 입력 순번 뒤 관련 변경으로 STALE 이 된 실행 (overlay: 에피소드는 모두 닫혀 있음)
_RECHECK_RUN = text(
    """
    SELECT r.analysis_run_id, r.company_id, r.delay_event_id, r.input_seq, r.stale_detected_seq
      FROM analysis_runs r
     WHERE r.is_latest AND r.status = 'STALE' AND r.completed_status = 'READY_FOR_REVIEW' AND r.stale_detected_seq IS NOT NULL
     ORDER BY r.stale_detected_seq, r.analysis_run_id
     LIMIT 1
    """
)
_REOPEN = "UPDATE delay_events SET closed_at = NULL, close_reason = NULL WHERE delay_event_id = :event_id"


class _SameSignature:
    """2차 재계산이 '서명 그대로'라고 답하는 가짜 재계산기"""

    async def signature_changed(self, run, changes):
        return False


async def _recheck_case(db):
    row = (await db.execute(_RECHECK_RUN)).first()
    if row is None:
        pytest.skip("재판정할 실행이 없는 DB 라 건너뜀")
    run_id, company_id, event_id, input_seq, stale_seq = row
    result = await _result_from_db(db, run_id)
    await _clear(db, run_id)
    await db.execute(
        text(_REOPEN), {"event_id": event_id}
    )  # 열린 에피소드여야 재판정 대상(닫히면 동결)
    db.expire_all()
    return run_id, company_id, input_seq, stale_seq, result


async def test_change_after_input_seq_makes_saved_result_stale(db_write_session):
    """분석하는 동안(입력 순번 뒤) 의존 범위에 걸린 변경이 있으면, 저장 직후 재판정으로 STALE (Q-13a)"""
    db = db_write_session
    run_id, company_id, input_seq, stale_seq, result = await _recheck_case(db)
    outcome = await save_analysis_result(
        db, company_id=company_id, analysis_run_id=run_id, result=result
    )
    assert (outcome.saved, outcome.status) == (True, "STALE")
    run = await db.scalar(
        select(AnalysisRun)
        .where(AnalysisRun.analysis_run_id == run_id)
        .execution_options(populate_existing=True)
    )
    assert (run.status, run.completed_status) == (
        "STALE",
        "READY_FOR_REVIEW",
    )  # 결과는 보존, ck25
    assert (
        input_seq < run.stale_detected_seq <= stale_seq
    )  # 재계산기가 없으면 1차에 처음 걸린 수집
    assert run.stale_detected_at == run.finished_at  # 같은 트랜잭션의 같은 DB 시각
    assert len(await _rows(db, InventoryImpactResult, run_id)) > 0


async def test_given_now_sets_finish_after_start(db_write_session):
    """이슈 #50(rouiv 조건 2): 넘긴 완료 시각을 쓰되 실행 시작 + 1초보다 이르지 않게. STALE 재판정 시각도 같은 값"""
    db = db_write_session
    run_id, company_id, *_, result = await _recheck_case(db)
    started_at = await db.scalar(
        select(AnalysisRun.started_at).where(AnalysisRun.analysis_run_id == run_id)
    )

    # 실행 생성과 같은 시각을 넘김 → 시작 + 1초
    await save_analysis_result(
        db,
        company_id=company_id,
        analysis_run_id=run_id,
        result=result,
        now=started_at,
    )

    run = await db.scalar(
        select(AnalysisRun)
        .where(AnalysisRun.analysis_run_id == run_id)
        .execution_options(populate_existing=True)
    )
    assert run.finished_at == started_at + timedelta(seconds=1)
    # 이 경우는 재판정으로 STALE(위 시험과 같은 입력) → 감지 시각 = 완료 시각
    assert (run.status, run.stale_detected_at) == ("STALE", run.finished_at)


async def test_given_later_now_is_used_as_is(db_write_session):
    db = db_write_session
    run_id, company_id, *_, result = await _recheck_case(db)
    started_at = await db.scalar(
        select(AnalysisRun.started_at).where(AnalysisRun.analysis_run_id == run_id)
    )
    later = started_at + timedelta(minutes=5)

    await save_analysis_result(
        db,
        company_id=company_id,
        analysis_run_id=run_id,
        result=result,
        recalculator=_SameSignature(),
        now=later,
    )

    finished = await db.scalar(
        select(AnalysisRun.finished_at)
        .where(AnalysisRun.analysis_run_id == run_id)
        .execution_options(populate_existing=True)
    )
    assert finished == later


async def test_recheck_keeps_result_when_signature_is_same(db_write_session):
    """2차 재계산이 서명 그대로면 STALE 로 바꾸지 않는다 (재계산기 주입)"""
    db = db_write_session
    run_id, company_id, *_, result = await _recheck_case(db)
    outcome = await save_analysis_result(
        db,
        company_id=company_id,
        analysis_run_id=run_id,
        result=result,
        recalculator=_SameSignature(),
    )
    assert (outcome.saved, outcome.status) == (True, "READY_FOR_REVIEW")
