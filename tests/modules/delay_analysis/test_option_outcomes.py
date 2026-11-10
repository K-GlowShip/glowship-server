import copy
import os
import uuid
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
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
from app.modules.delay_analysis.application.agent.tools.option_feasibility import (
    check_option_feasibility,
)
from app.modules.delay_analysis.application.agent.tools.option_outcomes import (
    calculate_option_outcomes,
)
from app.modules.delay_analysis.application.agent.tools.option_schemas import (
    CalculateOptionOutcomesInput,
    CheckOptionFeasibilityInput,
)
from app.modules.delay_analysis.infrastructure.analysis_run import AnalysisRun
from app.modules.delay_analysis.infrastructure.engine import engine_input as EI
from app.modules.delay_analysis.infrastructure.stored_json_schemas import SkuResults
from tests.modules.delay_analysis.evaluation.evaluation_adapters import (
    engine_outcomes,
)

"""
    T10 calculate_option_outcomes (docs/analysis/option-outcomes.md)
      - 단위 시험(엔진·DB 없음): 손으로 만든 작은 입력(주문 2건·SKU 2개)과 가짜 배정기로 판정 순서·사유·배정 입력·입력 대조를 본다.
      - 엔진 대조(엔진 경로 + overlay DB): 실행마다 T1~T9 를 그래프 순서로 부르고, T10 출력을 엔진 analysis["options"]를
        storage.py 와 같은 변환으로 옮긴 값과 대응안·SKU 항목·주문 결과까지 통째로 비교한다.
      - 정답 대조(GLOWSHIP_ENGINE_GOLDEN=1): overlay 에 저장된 response_options 39행(14실행)·option_order_outcomes 21행과 열마다 비교.
      - 평가 시나리오 10건 대조는 evaluation/test_evaluation_t10.py.
"""

D = date(2026, 3, 10)
E = date(2026, 3, 25)
H_END = date(2026, 5, 14)  # max(E, 약속일들) + 14
DEST, DONOR, KR = (str(uuid.UUID(int=i)) for i in (1, 2, 3))
SKU_A, SKU_B = (str(uuid.UUID(int=i)) for i in (11, 12))
O1, O2 = (str(uuid.UUID(int=i)) for i in (21, 22))
L1, L2 = (str(uuid.UUID(int=i)) for i in (31, 32))
ZERO_SHA = "0" * 64


def _path() -> dict:
    return {"stockout_date": None, "stockout_days": 0, "safety_stock_breach_date": None, "min_gap": "0.0000",
            "daily_deficits": []}  # fmt: skip


def _input(**lead_times) -> CalculateOptionOutcomesInput:
    """해상 AT_SEA(후보 KEEP·KR·OTHER). 수요 0·도착 재고 0이라 예측 원값은 모두 0이다.
    - O1: 지연 유발, SKU A 10개, 약속 03-20. A 는 이 주문 때문에 K_IMPACTED.
    - O2: 기존 위험, SKU B 5개, 약속 04-30. B 는 공여 창고 다음 입고가 없다.
    - E 시뮬레이션 요약: 두 주문 모두 확보되지 않고, O1 줄은 날마다 4개 보호."""
    lt = {"dest_outbound_handling_days": 1, "dest_customer_delivery_days": 2, "donor_outbound_handling_days": 1,
          "donor_customer_delivery_days": 3, "kr_outbound_handling_days": 1, "kr_air_transit_days": 2,
          "dest_inbound_handling_days": 1, **lead_times}  # fmt: skip
    wh = {
        "destination_warehouse": {"warehouse_id": DEST, "warehouse_code": "WH-W", "region_code": "WEST"},
        "donor_warehouse": {"warehouse_id": DONOR, "warehouse_code": "WH-E", "region_code": "EAST"},
        "kr_origin_warehouse": {"warehouse_id": KR, "warehouse_code": "WH-KR", "region_code": "KR"},
    }  # fmt: skip
    t1 = {"shipment_id": str(uuid.UUID(int=41)), "base_date": D, "planned_3pl_inbound_date": date(2026, 3, 15),
          "expected_3pl_inbound_date": E, "stage": "AT_SEA", "transport_mode": "OCEAN",
          "planned_kr_release_date": D, "trigger_shipment_schedule_snapshot_id": str(uuid.UUID(int=42)),
          "shipment_skus": [{"sku_id": SKU_A, "sku_code": "A", "delayed_quantity": 100}], **wh}  # fmt: skip
    items = lambda donor: [
        {"sku_code": "A", "on_hand_quantity": 100 if donor else 0, "reserved_quantity": 0},
        {"sku_code": "B", "on_hand_quantity": 0, "reserved_quantity": 0},
    ]  # fmt: skip
    t2 = {"inventory_date": D - timedelta(1), "kr_on_hand": [{"sku_code": "A", "on_hand_quantity": 50}],
          "warehouses": [{"warehouse_code": "WH-E", "items": items(True)}, {"warehouse_code": "WH-W", "items": items(False)}]}  # fmt: skip
    t3 = {"inbound": [], "donor_next_inbound": [{"sku_code": "A", "next_inbound_date": date(2026, 4, 1)},
                                                {"sku_code": "B", "next_inbound_date": None}]}  # fmt: skip
    t4 = {"sales_basis_type": "ORDER_DEMAND", "basis_start_date": D - timedelta(28), "basis_end_date": D - timedelta(1),
          "basis_days": 28, "demand_totals": [], "event_days": 0, "event_names": [], "calendar_version": "v1",
          "calendar_sha256": ZERO_SHA, "actual_known_days": [], "actual_demand": []}  # fmt: skip
    order = lambda oid, code, line, sku, sku_id, qty, at, promised: {
        "order_id": oid, "order_code": code, "warehouse_id": DEST, "sales_channel": "SEPHORA", "ordered_at": at,
        "promised_delivery_at": promised, "lines": [{"order_line_id": line, "line_no": 1, "sku_id": sku_id, "sku_code": sku,
                                                     "ordered_quantity": qty, "line_status": "ACTIVE"}]}  # fmt: skip
    state = lambda code, waiting: {
        "warehouse_code": code, "replay_result_sha256": ZERO_SHA, "pending_dispatch": [], "waiting_order_codes": waiting,
        "blocked_order_protection": [], "secured_waiting_for_dispatch": []}  # fmt: skip
    t5 = {"orders": [order(O1, "O1", L1, "A", SKU_A, 10, "2026-03-05T10:00:00Z", "2026-03-20T23:59:59Z"),
                     order(O2, "O2", L2, "B", SKU_B, 5, "2026-03-06T10:00:00Z", "2026-04-30T23:59:59Z")],
          "allocation_state": [state("WH-E", []), state("WH-W", ["O1", "O2"])]}  # fmt: skip
    t8 = {"operating_rule_set_id": str(uuid.UUID(int=51)), "version_no": 1, "safety_stock_days": 14,
          "sales_basis_days": 28, "lead_times": lt,
          "skus": [{"sku_id": SKU_A, "sku_code": "A", "emergency_air_eligible": True},
                   {"sku_id": SKU_B, "sku_code": "B", "emergency_air_eligible": True}]}  # fmt: skip
    t6 = {"items": [{"sku_id": SKU_A, "sku_code": "A", "sku_scope": "IMPACTED", "raw_baseline_shortage": "0.0000",
                     "raw_total_shortage": "0.0000", "raw_increment_shortage": "0.0000", "baseline_prepare_quantity": 0,
                     "increment_prepare_quantity": 0, "total_prepare_quantity": 0, "safety_stock_quantity": 0,
                     "baseline": _path(), "total": _path()}],
          "impacted_sku_codes": ["A"], "watch_sku_codes": [], "completed_status": "READY_FOR_REVIEW",
          "result_class": "READY_ORDER_RISK"}  # fmt: skip
    risk = lambda oid, code, line, sku, qty, at, promised, attribution, delivery, baseline: {
        "order_id": oid, "order_code": code, "sales_channel": "SEPHORA", "ordered_at": at, "promised_delivery_at": promised,
        "risk_attribution": attribution, "expected_delivery_date": delivery, "baseline_expected_delivery_date": baseline,
        "expected_beyond_horizon": True, "lines": [{"order_line_id": line, "line_no": 1, "sku_code": sku,
                                                    "open_quantity": qty, "wait_cause": "DIRECT_SKU_SHORTAGE"}]}  # fmt: skip
    beyond = H_END + timedelta(3)
    t7 = {"risks": [risk(O1, "O1", L1, "A", 10, "2026-03-05T10:00:00Z", "2026-03-20T23:59:59Z", "DELAY_INDUCED", beyond, date(2026, 3, 18)),
                    risk(O2, "O2", L2, "B", 5, "2026-03-06T10:00:00Z", "2026-04-30T23:59:59Z", "PRE_EXISTING", beyond, beyond)],
          "delay_induced_sku_codes": ["A"], "simulation_horizon_end": H_END,
          "expected_plan_simulation": {"secured_dates": [], "days": [
              {"date": D + timedelta(i), "lines": [{"order_code": "O1", "line_no": 1, "protected": 4},
                                                   {"order_code": "O2", "line_no": 1, "protected": 0}]}
              for i in range((H_END - D).days + 1)]}}  # fmt: skip
    data = {"delay_context": t1, "inventory_snapshot": t2, "inbound_schedule": t3, "sales_history": t4,
            "open_orders": t5, "operating_rules": t8, "inventory_impact": t6, "at_risk_orders": t7}  # fmt: skip
    models = {
        k: CalculateOptionOutcomesInput.model_fields[k].annotation.model_validate(v)
        for k, v in data.items()
    }
    t9 = check_option_feasibility(CheckOptionFeasibilityInput(
        transport_mode="OCEAN", stage="AT_SEA", base_date=D, expected_3pl_inbound_date=E, planned_kr_release_date=D,
        lead_times=models["operating_rules"].lead_times, impacted_sku_codes=["A"], skus=models["operating_rules"].skus,
        kr_on_hand=models["inventory_snapshot"].kr_on_hand, donor_next_inbound=models["inbound_schedule"].donor_next_inbound))  # fmt: skip
    return CalculateOptionOutcomesInput(option_feasibility=t9, **models)


def _allocator(secured: dict[str, date]):
    """가짜 배정기: 주어진 주문을 그날 확보로 내고, 보호 수량은 없다. 받은 입력을 calls 에 남긴다."""
    calls = []

    def simulate(first, schedule):
        calls.append((first, schedule))
        days = [first["date"]] + [s["date"] for s in schedule]
        return [
            {"date": day, "lines": [], "orders": {
                c: {"status": "SECURED_WAITING_DISPATCH", "secured_date": sd.isoformat()}
                for c, sd in secured.items() if sd.isoformat() == day}}
            for day in days
        ]  # fmt: skip

    return simulate, calls


def _options(result) -> dict:
    return {o.option_type.value: o for o in result.options}


def _lines(option) -> dict:
    return {o.order_line_id: o for o in option.outcomes}


def test_keep_plan_reads_the_t7_plan_simulation():
    simulate, _calls = _allocator({"O1": date(2026, 3, 14)})
    keep = _options(calculate_option_outcomes(_input(), simulate))["KEEP_PLAN"]
    assert (keep.feasibility_status, keep.reason_code, keep.expected_delivery_date) == (
        "FEASIBLE",
        None,
        E,
    )
    assert (
        keep.fully_recovered_orders,
        keep.not_recovered_orders,
        keep.item_secured_blocked_orders,
    ) == (0, 2, 1)
    assert (
        keep.delay_induced_recovered_orders,
        keep.reduction_committed_total,
        keep.securable_quantity,
    ) == (0, 0, 0)
    o1, o2 = _lines(keep)[L1], _lines(keep)[L2]
    # O1 은 판정 기준일(약속 − 3일 = 03-17)에 보호 4개 → 부분 확보, 도착 창고 기존 재고
    assert (o1.outcome, o1.not_recovered_reason, o1.line_securement_status, o1.secured_quantity) == (
        "NOT_RECOVERED", "NO_RECOVERY_EFFECT", "PARTIALLY_SECURED", 4)  # fmt: skip
    assert (o1.source_warehouse_id, o1.supply_source) == (DEST, "EXISTING_STOCK")
    assert (o2.line_securement_status, o2.source_warehouse_id, o2.supply_source) == (
        "NOT_SECURED",
        None,
        None,
    )


def test_kr_air_runs_the_shared_allocator_from_d():
    simulate, calls = _allocator({"O1": date(2026, 3, 14)})
    kr = _options(calculate_option_outcomes(_input(), simulate))["KR_AIR_REPLENISH"]
    ((first, schedule),) = calls  # 계산하는 대응안은 KR 하나뿐
    assert (first["date"], first["policy"], first["policy_version"]) == (
        "2026-03-10",
        "v5",
        "D00-B-b2-v1",
    )
    assert first["pending_dispatch"] == [] and first["on_hand_start"] == {
        "A": 0,
        "B": 0,
    }
    assert [o["order_code"] for o in first["orders"]] == ["O1", "O2"]
    assert first["orders"][0]["lines"] == [
        {"line_no": 1, "sku_code": "A", "ordered_quantity": 10, "shipped_quantity": 0, "line_status": "ACTIVE"}]  # fmt: skip
    # 지연 선적은 E 에 들어오고, 일별 부족이 0이라 한국 항공 수량은 0(추가 입고 없음). 구간은 D+1 ~ 구간 끝
    receipts = {s["date"]: s["receipts"] for s in schedule if s["receipts"]}
    assert receipts == {"2026-03-25": {"A": 100}}
    assert (schedule[0]["date"], schedule[-1]["date"]) == (
        "2026-03-11",
        H_END.isoformat(),
    )
    # O1 확보 03-14 → 배송 03-17 ≤ 약속 03-20 → 회복, 한국 창고 항공
    assert (
        kr.feasibility_status,
        kr.expected_delivery_date,
        kr.securable_quantity,
    ) == ("FEASIBLE", date(2026, 3, 14), 0)
    assert (
        kr.fully_recovered_orders,
        kr.delay_induced_recovered_orders,
        kr.not_recovered_orders,
    ) == (1, 1, 1)
    o1, o2 = _lines(kr)[L1], _lines(kr)[L2]
    assert (o1.outcome, o1.recovered_quantity, o1.expected_delivery_at) == (
        "RECOVERED",
        10,
        "2026-03-17T23:59:59Z",
    )
    assert (o1.source_warehouse_id, o1.supply_source) == (KR, "KR_AIR")
    assert (o2.outcome, o2.not_recovered_reason) == (
        "NOT_RECOVERED",
        "NO_RECOVERY_EFFECT",
    )


def test_pending_dispatch_goes_to_the_allocator_as_engine_json():
    """T5 출고 대기 주문(모델)은 엔진 restore.carry 모양 그대로(확보일 = 문자열) 첫날 입력에 들어가고, 그 확보일이 결과에 쓰인다."""
    body = _input().model_dump(mode="json")
    dest = body["open_orders"]["allocation_state"][1]
    assert dest["warehouse_code"] == "WH-W"
    dest["pending_dispatch"] = [
        {"order_code": "O1", "secured_date": "2026-03-09", "lines": [{"line_no": 1, "sku_code": "A", "quantity": 10}]}]  # fmt: skip
    dest["waiting_order_codes"] = ["O2"]
    data = CalculateOptionOutcomesInput.model_validate(body)
    (pending,) = data.open_orders.allocation_state[1].pending_dispatch
    assert pending.secured_date == date(2026, 3, 9)

    simulate, calls = _allocator({})
    kr = _options(calculate_option_outcomes(data, simulate))["KR_AIR_REPLENISH"]
    ((first, _schedule),) = calls
    assert first["pending_dispatch"] == dest["pending_dispatch"]
    assert [o["order_code"] for o in first["orders"]] == ["O2"]
    # O1 은 D−1 에 이미 확보 → 확보일 03-09 + 출고 1일 + 배송 2일 = 03-12 ≤ 약속 03-20
    o1 = _lines(kr)[L1]
    assert (o1.outcome, o1.expected_delivery_at) == (
        "RECOVERED",
        "2026-03-12T23:59:59Z",
    )


def test_other_region_fills_orders_from_the_donor_pool():
    simulate, _calls = _allocator({})
    other = _options(calculate_option_outcomes(_input(), simulate))[
        "OTHER_REGION_FULFILL"
    ]
    assert (other.feasibility_status, other.expected_delivery_date) == ("FEASIBLE", D)
    o1, o2 = _lines(other)[L1], _lines(other)[L2]
    # O1: 접수 03-05 → max(접수, D) + 1 + 3 = 03-14 배송, 공여 풀 100개에서 10개. O2: B 다음 입고 없음
    assert (o1.outcome, o1.expected_delivery_at, o1.supply_source, o1.source_warehouse_id) == (
        "RECOVERED", "2026-03-14T23:59:59Z", "OTHER_REGION_WAREHOUSE", DONOR)  # fmt: skip
    assert (o2.outcome, o2.not_recovered_reason) == (
        "NOT_RECOVERED",
        "DONOR_NO_NEXT_INBOUND",
    )
    (item,) = other.sku_results.items
    assert (
        item.supply_quantity,
        item.donor.transferable_quantity,
        item.donor.order_layer_used_quantity,
    ) == (10, 100, 10)
    assert other.securable_quantity == 10


def test_donor_unknown_only_in_risk_orders_needs_check():
    """판정 순서 7(9절 Q9): 모든 주문이 회복되지 않고 대표 사유가 DONOR_NO_NEXT_INBOUND 면 NEEDS_CHECK, 집계는 있음"""
    # 공여 → 도착 권역 배송 20일: O1 은 약속까지 닿지 않고(PROMISE_UNREACHABLE_LEADTIME), O2 는 B 다음 입고가 없음
    simulate, _calls = _allocator({})
    other = _options(
        calculate_option_outcomes(_input(donor_customer_delivery_days=20), simulate)
    )["OTHER_REGION_FULFILL"]
    assert (other.feasibility_status, other.reason_code) == (
        "NEEDS_CHECK",
        "DONOR_NO_NEXT_INBOUND",
    )
    assert (
        other.fully_recovered_orders,
        other.not_recovered_orders,
        other.reduction_committed_total,
    ) == (0, 2, 0)
    assert {k: v.not_recovered_reason for k, v in _lines(other).items()} == {
        L1: "PROMISE_UNREACHABLE_LEADTIME", L2: "DONOR_NO_NEXT_INBOUND"}  # fmt: skip


def test_missing_lead_times_are_not_calculated():
    simulate, calls = _allocator({})
    data = _input(kr_air_transit_days=None, donor_outbound_handling_days=None)
    got = _options(calculate_option_outcomes(data, simulate))
    for option_type in ("KR_AIR_REPLENISH", "OTHER_REGION_FULFILL"):
        o = got[option_type]
        assert (o.feasibility_status, o.reason_code) == (
            "NEEDS_CHECK",
            "MISSING_OPERATING_RULE",
        )
        assert o.outcomes == [] and o.sku_results.items == []
        assert (
            o.securable_quantity,
            o.reduction_committed_total,
            o.expected_delivery_date,
        ) == (None, None, None)
        assert o.fully_recovered_orders is None
    assert calls == [] and list(got) == [
        "KEEP_PLAN",
        "KR_AIR_REPLENISH",
        "OTHER_REGION_FULFILL",
    ]


def test_inputs_that_do_not_belong_together_stop():
    simulate, _calls = _allocator({})
    data = _input()
    wrong = copy.deepcopy(data)
    wrong.inventory_impact.items[0].raw_total_shortage = "1.0000"
    with pytest.raises(ValueError, match="T6"):
        calculate_option_outcomes(wrong, simulate)
    wrong = copy.deepcopy(data)
    wrong.option_feasibility.options[-1].precheck = "NEEDS_CHECK"
    wrong.option_feasibility.options[-1].reason_code = "DONOR_NO_NEXT_INBOUND"
    with pytest.raises(ValueError, match="T9"):
        calculate_option_outcomes(wrong, simulate)
    with pytest.raises(ValueError, match="보류"):
        calculate_option_outcomes(_input(dest_customer_delivery_days=None), simulate)


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


async def _inputs(session, run):
    """실행 하나를 그래프 노드와 같은 순서(T1~T5·T8 → T7 → T6 → T9)로 불러 T10 입력을 만든다.
    엔진 결과에 대응안이 없으면(NO_DELAY·보류·NO_RISK) None"""
    if await EI.judge_no_delay(session, run) is not None:
        return None
    built = await EI.build_engine_input(session, run)
    if isinstance(built, EI.EngineHold) or not built.analysis["options"]:
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
    t9 = check_option_feasibility(CheckOptionFeasibilityInput(
        transport_mode=t1.transport_mode, stage=t1.stage, base_date=t1.base_date,
        expected_3pl_inbound_date=t1.expected_3pl_inbound_date, planned_kr_release_date=t1.planned_kr_release_date,
        lead_times=t8.lead_times, impacted_sku_codes=t6.impacted_sku_codes, skus=t8.skus,
        kr_on_hand=t2.kr_on_hand, donor_next_inbound=t3.donor_next_inbound))  # fmt: skip
    data = CalculateOptionOutcomesInput(
        option_feasibility=t9, delay_context=t1, inventory_snapshot=t2, inbound_schedule=t3,
        sales_history=t4, open_orders=t5, operating_rules=t8, inventory_impact=t6, at_risk_orders=t7)  # fmt: skip
    return built, data


async def _option_runs(session):
    found = []
    for company in ("A_BEAUTY", "B_COSMETICS"):
        for run in await _company_runs(session, company):
            got = await _inputs(session, run)
            if got is not None:
                found.append((run, *got))
    return found


async def test_overlay_runs_match_engine(db_session, engine_dir):
    runs = await _option_runs(db_session)
    if not runs:
        pytest.skip("대응안이 있는 실행이 없는 DB 라 건너뜀")
    for run, built, data in runs:
        got = calculate_option_outcomes(data)
        expected = engine_outcomes(built.analysis)
        assert got.model_dump(mode="json") == expected.model_dump(mode="json"), (
            run.run_code
        )


# --- 정답 대조: overlay 저장 행 (선택 실행) -------------------------------------------------------

_OPTION_COLS = (
    "option_type", "feasibility_status", "reason_code", "required_quantity", "securable_quantity",
    "reduction_committed_total", "expected_delivery_date", "fully_recovered_orders", "delay_induced_recovered_orders",
    "item_secured_blocked_orders", "not_recovered_orders",
)  # fmt: skip
_OUTCOME_COLS = (
    "outcome", "not_recovered_reason", "line_securement_status", "secured_quantity", "recovered_quantity",
    "expected_delivery_at", "source_warehouse_id", "supply_source",
)  # fmt: skip


async def _stored_options(session, run):
    options = (await session.execute(text(
        f"SELECT {', '.join(_OPTION_COLS)}, sku_results FROM response_options WHERE analysis_run_id = :r"),
        {"r": run.analysis_run_id})).mappings()  # fmt: skip
    outcomes = (await session.execute(text(
        f"SELECT ro.option_type, ar.order_line_id, {', '.join('oo.' + c for c in _OUTCOME_COLS)}"
        " FROM option_order_outcomes oo JOIN response_options ro USING (response_option_id)"
        " JOIN at_risk_orders ar USING (at_risk_order_id) WHERE oo.analysis_run_id = :r"),
        {"r": run.analysis_run_id})).mappings()  # fmt: skip
    return [dict(r) for r in options], [dict(r) for r in outcomes]


def _jsonable(row: dict) -> dict:
    return {k: (str(v) if isinstance(v, uuid.UUID) else v.isoformat().replace("+00:00", "Z")
                if hasattr(v, "isoformat") else v) for k, v in row.items()}  # fmt: skip


def _tool_rows(result):
    options, outcomes = [], []
    for o in result.options:
        dumped = o.model_dump(mode="json")
        options.append(
            {
                **{c: dumped[c] for c in _OPTION_COLS},
                "sku_results": dumped["sku_results"],
            }
        )
        outcomes += [{"option_type": dumped["option_type"], "order_line_id": x["order_line_id"],
                      **{c: x[c] for c in _OUTCOME_COLS}} for x in dumped["outcomes"]]  # fmt: skip
    return options, outcomes


async def test_all_overlay_option_rows_match_golden(db_session, engine_dir):
    if os.environ.get("GLOWSHIP_ENGINE_GOLDEN") != "1":
        pytest.skip("GLOWSHIP_ENGINE_GOLDEN=1 일 때만 도는 정답 대조(몇 분)라 건너뜀")
    counted = {"runs": 0, "options": 0, "outcomes": 0}
    for run, _built, data in await _option_runs(db_session):
        stored_options, stored_outcomes = await _stored_options(db_session, run)
        if not stored_options:  # 엔진 하네스가 저장하지 않은 실행(시간 초과 주입 RT-1)
            assert run.failure_code == "TOOL_TIMEOUT_EXHAUSTED", run.run_code
            continue
        options, outcomes = _tool_rows(calculate_option_outcomes(data))
        key = lambda r: (r["option_type"], r.get("order_line_id", ""))
        stored = sorted(({**_jsonable(r), "sku_results": SkuResults.model_validate(r["sku_results"]).model_dump(mode="json")}
                         for r in stored_options), key=key)  # fmt: skip
        assert sorted(options, key=key) == stored, run.run_code
        assert sorted(outcomes, key=key) == sorted(
            (_jsonable(r) for r in stored_outcomes), key=key
        ), run.run_code
        counted["runs"] += 1
        counted["options"] += len(options)
        counted["outcomes"] += len(outcomes)
    assert counted == {"runs": 14, "options": 39, "outcomes": 21}
