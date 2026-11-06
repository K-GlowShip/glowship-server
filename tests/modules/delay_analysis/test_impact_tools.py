import copy
import os
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text

from app.core.config import get_settings
from app.modules.delay_analysis.application.agent.tools import impact_tools as IT
from app.modules.delay_analysis.application.agent.tools import lookup_tools as LT
from app.modules.delay_analysis.application.agent.tools.impact_schemas import (
    CalculateInventoryImpactInput,
    IdentifyAtRiskOrdersInput,
)
from app.modules.delay_analysis.application.agent.tools.lookup_schemas import (
    GetDelayContextInput,
)
from app.modules.delay_analysis.infrastructure.analysis_run import AnalysisRun
from app.modules.delay_analysis.infrastructure.engine import engine_input as EI
from app.modules.delay_analysis.infrastructure.engine.engine_loader import (
    load_engine_module,
)

"""
    재고 영향 계산 도구 T7·T6 (docs/analysis/impact-tools.md)
      - 단위 시험: 엔진·DB 없이 손으로 만든 엔진 결과로 옮기는 규칙(숫자 모양·inf→null·정렬·영향 집합·접수 시각·
        T10 용 시뮬레이션 요약·입력 확인)을 본다
      - 엔진 대조(엔진 경로 + overlay DB): DB 에 저장되지 않는 칸(min_gap·일별 부족·지연 유발 SKU·시뮬레이션 요약·접수 시각)을
        엔진 결과·판정 서명과 직접 대조한다
      - 정답 대조(GLOWSHIP_ENGINE_GOLDEN=1, 몇 분): overlay 에 저장된 inventory_impact_results 391행(18실행)·at_risk_orders 7행과
        엔진 storage.py 와 같은 변환으로 열마다 비교한다(Jira GLOW-126 인수 조건)
"""

SHIP = str(uuid.UUID(int=42))
DEST = str(uuid.UUID(int=1))
SKU_A, SKU_B, SKU_C = (str(uuid.UUID(int=i)) for i in (11, 12, 13))
O1, O2, O3 = (str(uuid.UUID(int=i)) for i in (71, 72, 73))
D = date(2026, 1, 10)


def _side(*, min_gap=-3.25, breach=None, so=None, so_days=0, ss=20):
    return {
        "stockout_date": so,
        "stockout_days": so_days,
        "breach_date": breach,
        "min_gap": min_gap,
        "ss": ss,
        # 엔진은 dict 를 날짜 순서로 만들지만 도구는 다시 정렬한다(순서를 뒤집어 둠)
        "daily": {D + timedelta(1): 2.000049999, D: 0.0},
    }


def _fc(raw_b="0.0000", raw_t="5.5000", raw_i="5.5000", bp=0, ip=6, **kw):
    return {
        "raw_baseline": Decimal(raw_b),
        "raw_total": Decimal(raw_t),
        "raw_increment": Decimal(raw_i),
        "baseline_prepare": bp,
        "increment_prepare": ip,
        "total_prepare": bp + ip,
        "base": _side(min_gap=float("inf")),
        "total": _side(**kw),
    }


def _risk(order_id, code, attribution, sku, *, wait=None, delivery_p=date(2026, 1, 14)):
    return {
        "order_id": order_id,
        "order_code": code,
        "channel": "AMAZON",
        "ordered": date(2026, 1, 8),
        "promised": date(2026, 1, 15),
        "promised_at": "2026-01-15T23:59:59Z",
        "attribution": attribution,
        "delivery_E": date(2026, 1, 20),
        "delivery_P": delivery_p,
        "beyond": False,
        "lines": [
            {"order_line_id": str(uuid.uuid5(uuid.UUID(order_id), "2")), "line_no": 2, "sku": "SKU-A",
             "open_quantity": 1, "wait_cause": None},
            {"order_line_id": str(uuid.uuid5(uuid.UUID(order_id), "1")), "line_no": 1, "sku": sku,
             "open_quantity": 3, "wait_cause": wait},
        ],
    }  # fmt: skip


def _fake(**over):
    """엔진 snapshot.build + pipeline.analyze 결과와 같은 키를 가진 작은 엔진 입력"""
    ol = {
        "risks": [
            _risk(O2, "A-ORD-2", "DELAY_INDUCED", "SKU-C", wait="DIRECT_SKU_SHORTAGE"),
            _risk(O1, "A-ORD-1", "PRE_EXISTING", "SKU-A", delivery_p=date(2026, 1, 16)),
        ],
        "h_end": date(2026, 2, 3),
        "simE": {
            "h_end": date(2026, 2, 3),
            "secured": {"A-ORD-1": date(2026, 1, 18), "A-ORD-9": date(2026, 1, 11)},
            "outs": {
                D + timedelta(1): {"lines": [
                    {"order_code": "A-ORD-9", "line_no": 1, "protected": 5},
                    {"order_code": "A-ORD-2", "line_no": 2, "protected": 1},
                    {"order_code": "A-ORD-2", "line_no": 1, "protected": 0},
                ]},
                D: {"lines": [{"order_code": "A-ORD-1", "line_no": 1, "protected": 2}]},
            },
        },
    }  # fmt: skip
    analysis = {
        "status": "READY_FOR_REVIEW",
        "result_class": "READY_ORDER_RISK",
        "fc": {"SKU-B": _fc(raw_i="0.0000", ip=0), "SKU-A": _fc(), "SKU-C": _fc(raw_t="0.0000", raw_i="0.0000", ip=0)},
        "ol": ol,
        "k_imp": ["SKU-A", "SKU-C"],
        "k_watch": ["SKU-B"],
    }  # fmt: skip
    orders = [
        {"order_id": O1, "ordered_at": "2026-01-08T03:04:05Z"},
        {"order_id": O2, "ordered_at": "2026-01-08T09:00:00Z"},
        {"order_id": O3, "ordered_at": "2026-01-09T00:00:00Z"},
    ]
    snap = {
        "delayed_shipment_id": SHIP,
        "D": D,
        "dest": {"warehouse_id": DEST},
        "restore": {DEST: {"open_orders": orders}},
        "sku_ids": {"SKU-A": SKU_A, "SKU-B": SKU_B, "SKU-C": SKU_C},
        # SKU-C 는 지연 선적에 없지만 DELAY_INDUCED 주문 상세에 있어 엔진이 fc 에 더한 SKU(pipeline.analyze 70~72행)
        "k_shipment": ["SKU-A", "SKU-B"],
    }
    analysis.update(over)
    return SimpleNamespace(engine=SimpleNamespace(snap=snap, analysis=analysis))


def _payload(shipment=SHIP, base=D, induced=("SKU-A", "SKU-C")):
    return SimpleNamespace(
        delay_context=SimpleNamespace(shipment_id=shipment, base_date=base),
        delay_induced_sku_codes=list(induced),
    )


# --- 단위 시험 --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("5.5000"), "5.5000"),
        (2.000049999, "2.0000"),
        (2.00005, "2.0000"),  # half-even (엔진 q4 와 같음)
        (2.00015, "2.0002"),
        (-1e-14, "0.0000"),  # 부동소수 잡음의 -0.0000 은 0.0000
        (-3.25, "-3.2500"),
    ],
)
def test_raw4_matches_engine_q4_rule(value, expected):
    assert IT._raw4(value) == expected


async def test_t7_moves_order_layer_and_sorts():
    out = await IT.identify_at_risk_orders(_fake(), _payload())

    assert [r.order_code for r in out.risks] == ["A-ORD-1", "A-ORD-2"]
    first = out.risks[0]
    assert (
        first.ordered_at == "2026-01-08T03:04:05Z"
    )  # 엔진 ol 의 날짜가 아니라 스냅샷 원래 시각
    assert (
        first.risk_attribution == "PRE_EXISTING"
        and first.baseline_expected_delivery_date == date(2026, 1, 16)
    )
    assert [ln.line_no for ln in first.lines] == [1, 2]
    assert out.risks[1].lines[0].wait_cause == "DIRECT_SKU_SHORTAGE"
    assert out.delay_induced_sku_codes == [
        "SKU-A",
        "SKU-C",
    ]  # DELAY_INDUCED 주문의 모든 활성 상세 SKU
    assert out.simulation_horizon_end == date(2026, 2, 3)


async def test_t7_plan_simulation_keeps_only_at_risk_orders():
    sim = (
        await IT.identify_at_risk_orders(_fake(), _payload())
    ).expected_plan_simulation

    assert [(s.order_code, s.secured_date) for s in sim.secured_dates] == [
        ("A-ORD-1", date(2026, 1, 18))
    ]
    assert [d.date for d in sim.days] == [D, D + timedelta(1)]
    assert [(ln.order_code, ln.line_no, ln.protected) for ln in sim.days[1].lines] == [
        ("A-ORD-2", 1, 0), ("A-ORD-2", 2, 1)]  # fmt: skip


async def test_t6_moves_forecast_layer():
    out = await IT.calculate_inventory_impact(_fake(), _payload())

    assert [i.sku_code for i in out.items] == ["SKU-A", "SKU-B", "SKU-C"]
    assert [i.sku_scope for i in out.items] == ["IMPACTED", "WATCH", "IMPACTED"]
    a = out.items[0]
    assert (
        a.raw_total_shortage,
        a.total_prepare_quantity,
        a.safety_stock_quantity,
    ) == ("5.5000", 6, 20)
    assert a.baseline.min_gap is None  # inf → null (9절 Q10)
    assert a.total.min_gap == "-3.2500"
    assert [(d.date, d.deficit) for d in a.total.daily_deficits] == [
        (D, "0.0000"),
        (D + timedelta(1), "2.0000"),
    ]
    assert (out.completed_status, out.result_class) == (
        "READY_FOR_REVIEW",
        "READY_ORDER_RISK",
    )


async def test_t6_keeps_delay_induced_sku_outside_shipment():
    # overlay 데이터에는 이 경우가 없어 정답 대조로는 확인되지 않는다(11-06 변이 시험에서 확인). 그래서 단위 시험으로 고정한다
    out = await IT.calculate_inventory_impact(_fake(), _payload())
    c = next(i for i in out.items if i.sku_code == "SKU-C")
    assert c.sku_scope == "IMPACTED" and c.increment_prepare_quantity == 0
    assert "SKU-C" in out.impacted_sku_codes


@pytest.mark.parametrize(
    "payload",
    [
        _payload(shipment=str(uuid.UUID(int=43))),
        _payload(base=D + timedelta(1)),
    ],
    ids=["다른 선적", "다른 기준일"],
)
async def test_tools_reject_payload_of_another_run(payload):
    with pytest.raises(ValueError, match="이 실행의 엔진 입력"):
        await IT.identify_at_risk_orders(_fake(), payload)
    with pytest.raises(ValueError, match="이 실행의 엔진 입력"):
        await IT.calculate_inventory_impact(_fake(), payload)


async def test_t6_rejects_delay_induced_skus_that_differ_from_engine():
    with pytest.raises(ValueError, match="지연 유발 SKU"):
        await IT.calculate_inventory_impact(_fake(), _payload(induced=("SKU-A",)))


async def test_tools_refuse_runs_without_forecast_or_order_layer():
    ctx = _fake(status="NO_DELAY", ol=None, fc={})
    with pytest.raises(ValueError, match="예측 층·주문 층이 없습니다"):
        await IT.identify_at_risk_orders(ctx, _payload())


async def test_unknown_wait_cause_fails_output_model():
    ctx = _fake()
    ctx.engine.analysis["ol"]["risks"][0]["lines"][1]["wait_cause"] = (
        "V4_BLOCKED_BY_PRIOR_ORDER"
    )
    with pytest.raises(
        ValidationError
    ):  # 배정기 v4 호환 경로의 값. 엔진은 v5 라 내지 않지만, 오면 멈춘다(DDL ck4)
        await IT.identify_at_risk_orders(ctx, _payload())


async def test_tools_do_not_change_engine_result():
    ctx = _fake()
    before = copy.deepcopy(ctx.engine.analysis)
    first = (
        await IT.identify_at_risk_orders(ctx, _payload()),
        await IT.calculate_inventory_impact(ctx, _payload()),
    )
    again = (
        await IT.identify_at_risk_orders(ctx, _payload()),
        await IT.calculate_inventory_impact(ctx, _payload()),
    )
    assert first == again and ctx.engine.analysis == before


# --- 실제 엔진 + 개발 DB -----------------------------------------------------------------------


@pytest.fixture
def engine_dir():
    if get_settings().glowship_engine_dir is None:
        pytest.skip("GLOWSHIP_ENGINE_DIR 이 없어 실제 엔진 시험을 건너뜀")


async def _company_runs(session, company_code):
    company_id = await session.scalar(
        text("SELECT company_id FROM companies WHERE company_code = :c"),
        {"c": company_code},
    )
    if company_id is None:
        pytest.skip(f"고객사 {company_code} 가 없는 DB(overlay 미적재)라 건너뜀")
    rows = await session.scalars(
        select(AnalysisRun).where(AnalysisRun.company_id == company_id).order_by(AnalysisRun.started_at, AnalysisRun.run_code)
    )  # fmt: skip
    return list(rows)


async def _tools(session, run):
    """실행 하나를 그래프 노드와 같은 순서(T1~T5·T8 → T7 → T6)로 부른다. 엔진 결과가 없으면(NO_DELAY·보류) None"""
    if await EI.judge_no_delay(session, run) is not None:
        return None
    built = await EI.build_engine_input(session, run)
    if isinstance(built, EI.EngineHold):
        return None
    ctx = SimpleNamespace(engine=built)
    t1 = await LT.get_delay_context(ctx, GetDelayContextInput(
        analysis_run_id=str(run.analysis_run_id), delay_event_id=str(run.delay_event_id),
        base_date=built.base_date, input_seq=run.input_seq))  # fmt: skip
    t2, t3, t4, t5, t8 = [await f(ctx, None) for f in (LT.get_inventory_snapshot, LT.get_inbound_schedule,
                                                       LT.get_sales_history, LT.get_open_orders, LT.get_operating_rules)]  # fmt: skip
    t7 = await IT.identify_at_risk_orders(ctx, IdentifyAtRiskOrdersInput(
        delay_context=t1, inventory_snapshot=t2, inbound_schedule=t3, open_orders=t5, operating_rules=t8))  # fmt: skip
    t6 = await IT.calculate_inventory_impact(ctx, CalculateInventoryImpactInput(
        delay_context=t1, inventory_snapshot=t2, inbound_schedule=t3, sales_history=t4, open_orders=t5,
        operating_rules=t8, delay_induced_sku_codes=t7.delay_induced_sku_codes))  # fmt: skip
    return built, t7, t6


def _check_against_engine(built, t7, t6):
    """DB 에 저장되지 않는 칸을 엔진 결과·판정 서명과 대조한다"""
    a, snap = built.analysis, built.snap
    common, core = load_engine_module("common"), load_engine_module("core")
    for item in t6.items:
        v = a["fc"][item.sku_code]
        for side, path in (("base", item.baseline), ("total", item.total)):
            mg = v[side]["min_gap"]
            assert path.min_gap == (None if mg == float("inf") else IT._raw4(mg))
            if mg != float("inf"):
                assert Decimal(path.min_gap) == common.q4(mg)
            assert [(d.date, d.deficit) for d in path.daily_deficits] == [
                (day, common.s4(x)) for day, x in sorted(v[side]["daily"].items())]  # fmt: skip
            assert [d.date for d in path.daily_deficits] == common.days(
                snap["D"], snap["E"]
            )
        assert item.increment_prepare_quantity == v["increment_prepare"]
    assert (t6.completed_status, t6.result_class) == (a["status"], a["result_class"])
    assert t7.delay_induced_sku_codes == sorted(core.delay_induced_skus(a["ol"]))
    # 판정 서명과 같은 값인지(서명은 같은 엔진 결과에서 만들어짐)
    sig = a["signature"]
    assert sig["k_impacted"] == sorted(
        snap["sku_ids"][s] for s in t6.impacted_sku_codes
    )
    assert sig["sku_increments"] == sorted(
        ({"sku_id": i.sku_id, "increment_prepare_quantity": i.increment_prepare_quantity}
         for i in t6.items if i.sku_code in t6.impacted_sku_codes), key=lambda x: x["sku_id"])  # fmt: skip
    assert sig["at_risk"] == sorted(
        ({"order_line_id": ln.order_line_id, "risk_attribution": r.risk_attribution} for r in t7.risks for ln in r.lines),
        key=lambda x: x["order_line_id"])  # fmt: skip
    # T10 용 시뮬레이션 요약 = 엔진 simE 의 위험 주문 부분
    sim, codes = a["ol"]["simE"], {r.order_code for r in t7.risks}
    plan = t7.expected_plan_simulation
    assert {s.order_code: s.secured_date for s in plan.secured_dates} == {
        c: d for c, d in sim["secured"].items() if c in codes}  # fmt: skip
    assert [d.date for d in plan.days] == sorted(sim["outs"])
    for day in plan.days:
        assert {(ln.order_code, ln.line_no, ln.protected) for ln in day.lines} == {
            (x["order_code"], x["line_no"], x["protected"]) for x in sim["outs"][day.date]["lines"] if x["order_code"] in codes}  # fmt: skip
    # 접수 시각 = 스냅샷 원래 시각(날짜가 엔진 ol 과 같음)
    for r in t7.risks:
        engine_risk = next(
            x for x in a["ol"]["risks"] if x["order_code"] == r.order_code
        )
        assert r.ordered_at[:10] == engine_risk["ordered"].isoformat()


def _by_code(runs, code, status):
    found = [r for r in runs if r.run_code == code and r.completed_status == status]
    if not found:
        pytest.skip(f"실행 {code}({status}) 가 없는 DB 라 건너뜀")
    return found[0]


async def test_at_risk_run_matches_engine_and_stored_rows(db_session, engine_dir):
    run = _by_code(
        await _company_runs(db_session, "A_BEAUTY"), "AN-260115-001", "READY_FOR_REVIEW"
    )
    built, t7, t6 = await _tools(db_session, run)
    _check_against_engine(built, t7, t6)
    assert sum(len(r.lines) for r in t7.risks) == 7  # 저장된 위험 주문 7행
    await _compare_with_stored(db_session, run, t7, t6)


async def test_no_risk_run_matches_engine_and_stored_rows(db_session, engine_dir):
    run = _by_code(
        await _company_runs(db_session, "B_COSMETICS"), "AN-260117-001", "NO_RISK"
    )
    built, t7, t6 = await _tools(db_session, run)
    _check_against_engine(built, t7, t6)
    assert t7.risks == [] and t6.completed_status == "NO_RISK"
    await _compare_with_stored(db_session, run, t7, t6)


# --- 저장 행과 비교 (엔진 storage.py 86~111행과 같은 변환) ---------------------------------------

_IMPACT_COLS = (
    "sku_id", "sku_scope", "safety_stock_quantity", "raw_baseline_shortage_quantity", "raw_shortage_quantity",
    "raw_increment_shortage_quantity", "baseline_safety_stock_breach_date", "baseline_stockout_date",
    "baseline_stockout_days", "baseline_shortage_quantity", "safety_stock_breach_date", "stockout_date",
    "stockout_days", "shortage_quantity",
)  # fmt: skip
_RISK_COLS = (
    "order_line_id", "open_quantity", "promised_delivery_at", "expected_delivery_at",
    "baseline_expected_delivery_at", "expected_beyond_horizon", "risk_attribution", "wait_cause",
)  # fmt: skip


def _end_of_day(day):
    return datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=UTC)


def _impact_rows(t6):
    return sorted(
        (
            {
                "sku_id": uuid.UUID(i.sku_id), "sku_scope": i.sku_scope, "safety_stock_quantity": i.safety_stock_quantity,
                "raw_baseline_shortage_quantity": Decimal(i.raw_baseline_shortage),
                "raw_shortage_quantity": Decimal(i.raw_total_shortage),
                "raw_increment_shortage_quantity": Decimal(i.raw_increment_shortage),
                "baseline_safety_stock_breach_date": i.baseline.safety_stock_breach_date,
                "baseline_stockout_date": i.baseline.stockout_date, "baseline_stockout_days": i.baseline.stockout_days,
                "baseline_shortage_quantity": i.baseline_prepare_quantity,
                "safety_stock_breach_date": i.total.safety_stock_breach_date, "stockout_date": i.total.stockout_date,
                "stockout_days": i.total.stockout_days, "shortage_quantity": i.total_prepare_quantity,
            }
            for i in t6.items
        ),
        key=lambda r: str(r["sku_id"]),
    )  # fmt: skip


def _risk_rows(t7):
    return sorted(
        (
            {
                "order_line_id": uuid.UUID(ln.order_line_id), "open_quantity": ln.open_quantity,
                "promised_delivery_at": datetime.fromisoformat(r.promised_delivery_at),
                "expected_delivery_at": _end_of_day(r.expected_delivery_date),
                "baseline_expected_delivery_at": _end_of_day(r.baseline_expected_delivery_date),
                "expected_beyond_horizon": r.expected_beyond_horizon, "risk_attribution": r.risk_attribution,
                "wait_cause": ln.wait_cause,
            }
            for r in t7.risks
            for ln in r.lines
        ),
        key=lambda r: str(r["order_line_id"]),
    )  # fmt: skip


async def _stored(session, table, cols, run, key):
    rows = (
        await session.execute(
            text(
                f"SELECT {', '.join(cols)} FROM {table} WHERE analysis_run_id = :r ORDER BY {key}::text"
            ),
            {"r": run.analysis_run_id},
        )
    ).mappings()
    return [dict(r) for r in rows]


async def _compare_with_stored(session, run, t7, t6):
    stored_impact = await _stored(
        session, "inventory_impact_results", _IMPACT_COLS, run, "sku_id"
    )
    stored_risk = await _stored(
        session, "at_risk_orders", _RISK_COLS, run, "order_line_id"
    )
    assert _impact_rows(t6) == stored_impact, run.run_code
    assert _risk_rows(t7) == stored_risk, run.run_code
    return len(stored_impact), len(stored_risk)


# --- 정답 대조: overlay 391행·7행 (선택 실행) ----------------------------------------------------

# 저장 행이 없는 실행: NO_DELAY·보류는 엔진 결과가 없고, 시간 초과 주입 실행(RT-1)은 엔진 하네스가 결과를 지운다(run_s1_s5.py 169~171행)
_NO_ROWS_STATUS = {"NO_DELAY", "ON_HOLD"}
_NO_ROWS_FAILURE = "TOOL_TIMEOUT_EXHAUSTED"


async def test_all_overlay_rows_match_golden(db_session, engine_dir):
    if os.environ.get("GLOWSHIP_ENGINE_GOLDEN") != "1":
        pytest.skip("GLOWSHIP_ENGINE_GOLDEN=1 일 때만 도는 정답 대조(몇 분)라 건너뜀")
    counted = {"impact": 0, "risk": 0, "runs": 0}
    for company in ("A_BEAUTY", "B_COSMETICS"):
        for run in await _company_runs(db_session, company):
            stored = await db_session.scalar(
                text("SELECT count(*) FROM inventory_impact_results WHERE analysis_run_id = :r"), {"r": run.analysis_run_id}
            )  # fmt: skip
            if stored == 0:
                assert (
                    run.completed_status in _NO_ROWS_STATUS
                    or run.failure_code == _NO_ROWS_FAILURE
                ), run.run_code
                continue
            built, t7, t6 = await _tools(db_session, run)
            _check_against_engine(built, t7, t6)
            n_impact, n_risk = await _compare_with_stored(db_session, run, t7, t6)
            counted["impact"] += n_impact
            counted["risk"] += n_risk
            counted["runs"] += 1
            if run.completed_status in ("READY_FOR_REVIEW", "NO_RISK"):
                assert (t6.completed_status, t6.result_class) == (
                    run.completed_status,
                    run.result_class,
                ), run.run_code
    assert counted == {"impact": 391, "risk": 7, "runs": 18}  # Jira GLOW-126 인수 조건
