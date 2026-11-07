import copy
import os
import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.core.config import get_settings
from app.modules.delay_analysis.application.agent.tools import lookup_tools as LT
from app.modules.delay_analysis.application.agent.tools.lookup_schemas import (
    GetDelayContextInput,
)
from app.modules.delay_analysis.infrastructure.analysis_run import AnalysisRun
from app.modules.delay_analysis.infrastructure.engine import engine_input as EI

"""
    엔진 입력과 조회 도구 T1~T5·T8 (docs/analysis/engine-input.md)
      - 단위 시험: 엔진·DB 없이 손으로 만든 스냅샷으로 옮기는 규칙(정렬·형식·입력 확인·리드타임 null·불리언)을 본다
      - DB 시험: 엔진 경로 + 개발 DB(overlay 적재). 실행 3건(NO_DELAY·ON_HOLD·READY)을 실제로 만들어
        저장된 판정 서명·보류 코드와 같은지, 도구 출력이 엔진 값과 맞는지 본다
      - 정답 대조: GLOWSHIP_ENGINE_GOLDEN=1 일 때만(몇 분). overlay 실행 21건 모두
"""

W_DEST, W_DONOR, W_KR = (str(uuid.UUID(int=i)) for i in (1, 2, 3))
SKU_A, SKU_B = (str(uuid.UUID(int=i)) for i in (11, 12))
RULE = str(uuid.UUID(int=21))
COMPANY = str(uuid.UUID(int=31))
O1, O2 = (str(uuid.UUID(int=i)) for i in (71, 72))


def _lead(ltype, wh, days, to_region="", basis="CALENDAR_DAY"):
    return {"company_id": COMPANY, "operating_rule_set_id": RULE, "lead_time_type": ltype, "warehouse_id": wh,
            "to_region_code": to_region, "days": str(days), "day_basis": basis}  # fmt: skip


def _fake_engine(**lead_overrides):
    """엔진 snapshot.build 결과와 같은 키를 가진 작은 스냅샷(도착 WH-WEST, 공여 WH-EAST, SKU 2개)."""
    dest = {"warehouse_id": W_DEST, "warehouse_code": "WH-WEST", "region_code": "WEST"}
    donor = {
        "warehouse_id": W_DONOR,
        "warehouse_code": "WH-EAST",
        "region_code": "EAST",
    }
    kr = {"warehouse_id": W_KR, "warehouse_code": "KR-ORIGIN", "region_code": "KR"}
    order = {"order_id": O1, "order_code": "A-ORD-2", "warehouse_id": W_DEST, "sales_channel": "AMAZON",
             "ordered_at": "2026-01-01T03:00:00Z", "promised_delivery_at": "2026-01-05T00:00:00Z"}  # fmt: skip
    order2 = {**order, "order_id": O2, "order_code": "A-ORD-1", "warehouse_id": W_DONOR}
    rest = lambda: {
        "on_hand": {"SKU-B": 5, "SKU-A": 7}, "reserved": {"SKU-A": 1, "SKU-B": 0},
        "carry": [{"order_code": "A-ORD-9", "company_code": "A", "warehouse_code": "WH-WEST", "secured_date": "2026-01-01",
                   "lines": [{"line_no": 1, "sku_code": "SKU-A", "quantity": 1}]}],
        "waiting": [order], "open_orders": [], "protection": {"SKU-B": 0, "SKU-A": 2}, "secured_wait": {"SKU-A": 1},
        "result_sha256": "a" * 64,
    }  # fmt: skip
    restore = {W_DEST: rest(), W_DONOR: rest()}
    restore[W_DEST]["open_orders"] = [order]
    restore[W_DONOR]["open_orders"] = [order2]
    line = lambda oid, no, sku, status="ACTIVE": {"order_line_id": str(uuid.uuid5(uuid.UUID(oid), str(no))), "line_no": str(no), "sku_id": sku,
                                                  "ordered_quantity": "3", "line_status": status}  # fmt: skip
    leads = {
        "oh_dest": _lead("OUTBOUND_HANDLING", W_DEST, 1), "cd_same": _lead("CUSTOMER_DELIVERY", W_DEST, 2, "WEST"),
        "oh_donor": _lead("OUTBOUND_HANDLING", W_DONOR, 1), "cd_cross": _lead("CUSTOMER_DELIVERY", W_DONOR, 4, "WEST"),
        "oh_kr": _lead("OUTBOUND_HANDLING", W_KR, 1), "air_kr": _lead("AIR_TRANSIT", W_KR, 3, "WEST"),
        "inbound_dest": _lead("INBOUND_HANDLING", W_DEST, 1),
    }  # fmt: skip
    leads.update(lead_overrides)
    snap = {
        "company_id": COMPANY, "D": date(2026, 1, 10), "P": date(2026, 1, 12), "E": date(2026, 1, 20), "stage": "AT_SEA",
        "mode": "OCEAN", "planned_kr_release": date(2025, 12, 20), "trigger_snapshot_id": str(uuid.UUID(int=41)),
        "delayed_shipment_id": str(uuid.UUID(int=42)), "delayed_qty": {"SKU-B": 30},
        "dest": dest, "donor": donor, "kr": kr, "k_shipment": ["SKU-B"],
        "sku_ids": {"SKU-A": SKU_A, "SKU-B": SKU_B}, "sku_code_by_id": {SKU_A: "SKU-A", SKU_B: "SKU-B"},
        "sku_attr": {"SKU-B": {"emergency_air_eligible": "false"}, "SKU-A": {"emergency_air_eligible": "true"}},
        "restore": restore, "kr_on_hand": {"SKU-B": 9},
        "lines_by_order": {O1: [line(O1, 2, SKU_B, "REMOVED_AT_SOURCE"), line(O1, 1, SKU_A)], O2: [line(O2, 1, SKU_B)]},
        "inbound": {
            W_DEST: [{"shipment_id": str(uuid.UUID(int=52)), "arrival": date(2026, 1, 10), "raw_expected": date(2026, 1, 8),
                      "qty": {"SKU-B": 4, "SKU-A": 2}}],
            W_DONOR: [{"shipment_id": str(uuid.UUID(int=51)), "arrival": date(2026, 1, 15), "raw_expected": date(2026, 1, 15),
                       "qty": {"SKU-A": 6}}],
        },
        "next_inbound": {"SKU-A": date(2026, 1, 15), "SKU-B": None},
        "sales_basis": {"start": date(2025, 12, 13), "end": date(2026, 1, 9), "event_days": 0, "event_names": [],
                        "calendar_version": "cal-v1", "calendar_sha256": "b" * 64},
        "actual_demand": {}, "actual_known_days": [],
        "rule": {"operating_rule_set_id": RULE, "version_no": "3"}, "safety_days": 7, "basis_days": 28,
        "lt": {k: (int(r["days"]) if r else None) for k, r in leads.items()},
    }  # fmt: skip
    demand_ch = defaultdict(int)
    demand_ch[(W_DEST, SKU_A, "2025-12-13", "AMAZON")] = 5
    demand_ch[(W_DEST, SKU_A, "2026-01-09", "AMAZON")] = 2
    demand_ch[(W_DEST, SKU_A, "2026-01-10", "AMAZON")] = 100  # D 당일: 28일 창 밖
    demand_ch[(W_DEST, SKU_A, "2025-12-12", "AMAZON")] = 100  # D−29: 창 밖
    store = SimpleNamespace(
        demand_ch=demand_ch,
        t={"operating_lead_times": [r for r in leads.values() if r is not None]},
    )
    engine = SimpleNamespace(
        analysis_run_id=uuid.UUID(int=61), delay_event_id=uuid.UUID(int=62), base_date=snap["D"], input_seq=10,
        snap=snap, store=store, analysis={},
    )  # fmt: skip
    return SimpleNamespace(engine=engine)


def _t1_input(ctx, **over):
    e = ctx.engine
    values = {"analysis_run_id": str(e.analysis_run_id), "delay_event_id": str(e.delay_event_id),
              "base_date": e.base_date, "input_seq": e.input_seq}  # fmt: skip
    return GetDelayContextInput(**{**values, **over})


# --- 단위 시험 --------------------------------------------------------------------------------


async def test_t1_copies_delay_context_and_rejects_other_run():
    ctx = _fake_engine()
    out = await LT.get_delay_context(ctx, _t1_input(ctx))

    assert out.expected_3pl_inbound_date == date(2026, 1, 20)
    assert [(s.sku_code, s.delayed_quantity) for s in out.shipment_skus] == [
        ("SKU-B", 30)
    ]
    assert out.destination_warehouse.warehouse_code == "WH-WEST"
    assert out.kr_origin_warehouse.region_code == "KR"
    with pytest.raises(ValueError, match="T1 입력이 이 실행의 엔진 입력과 다릅니다"):
        await LT.get_delay_context(ctx, _t1_input(ctx, input_seq=11))
    with pytest.raises(ValueError, match="T1 입력"):
        await LT.get_delay_context(ctx, _t1_input(ctx, base_date=date(2026, 1, 11)))


async def test_t2_t3_sort_by_contract_keys():
    ctx = _fake_engine()
    inv = await LT.get_inventory_snapshot(ctx, None)
    inb = await LT.get_inbound_schedule(ctx, None)

    assert inv.inventory_date == date(2026, 1, 9)
    assert [w.warehouse_code for w in inv.warehouses] == ["WH-EAST", "WH-WEST"]
    assert [(i.sku_code, i.on_hand_quantity, i.reserved_quantity) for i in inv.warehouses[0].items] == [
        ("SKU-A", 7, 1), ("SKU-B", 5, 0)]  # fmt: skip
    assert [(k.sku_code, k.on_hand_quantity) for k in inv.kr_on_hand] == [("SKU-B", 9)]
    assert [(x.warehouse_code, str(x.arrival_date)) for x in inb.inbound] == [
        ("WH-EAST", "2026-01-15"), ("WH-WEST", "2026-01-10")]  # fmt: skip
    assert inb.inbound[1].expected_3pl_inbound_date == date(
        2026, 1, 8
    )  # 과거 예상일은 도착일만 D
    assert [q.sku_code for q in inb.inbound[1].quantities] == ["SKU-A", "SKU-B"]
    assert [(n.sku_code, n.next_inbound_date) for n in inb.donor_next_inbound] == [
        ("SKU-A", date(2026, 1, 15)), ("SKU-B", None)]  # fmt: skip


async def test_t4_counts_integer_totals_inside_28_day_window_without_touching_store():
    ctx = _fake_engine()
    before = dict(ctx.engine.store.demand_ch)
    out = await LT.get_sales_history(ctx, None)

    rows = {
        (t.warehouse_code, t.sku_code, t.sales_channel): t.quantity
        for t in out.demand_totals
    }
    assert len(rows) == 2 * 2 * 2  # 창고 2 × SKU 2 × 채널 2, 0 인 칸도 있음
    assert rows[("WH-WEST", "SKU-A", "AMAZON")] == 7  # D−28·D−1 만. D 당일·D−29 는 뺌
    assert rows[("WH-EAST", "SKU-B", "SEPHORA")] == 0
    assert [
        (t.warehouse_code, t.sku_code, t.sales_channel) for t in out.demand_totals
    ] == sorted(rows)
    assert (
        dict(ctx.engine.store.demand_ch) == before
    )  # defaultdict 에 열쇠가 생기지 않음
    assert out.basis_days == 28 and out.actual_demand == []


async def test_t5_lists_open_orders_and_allocation_state():
    ctx = _fake_engine()
    out = await LT.get_open_orders(ctx, None)

    assert [o.order_code for o in out.orders] == ["A-ORD-1", "A-ORD-2"]
    lines = out.orders[1].lines
    assert [(ln.line_no, ln.sku_code, ln.line_status) for ln in lines] == [
        (1, "SKU-A", "ACTIVE"), (2, "SKU-B", "REMOVED_AT_SOURCE")]  # fmt: skip
    state = out.allocation_state
    assert [s["warehouse_code"] for s in state] == ["WH-EAST", "WH-WEST"]
    assert state[1]["pending_dispatch"] == [
        {"order_code": "A-ORD-9", "secured_date": "2026-01-01", "lines": [{"line_no": 1, "sku_code": "SKU-A", "quantity": 1}]}]  # fmt: skip
    assert state[1]["waiting_order_codes"] == ["A-ORD-2"]
    assert state[1]["blocked_order_protection"] == [
        {"sku_code": "SKU-A", "quantity": 2}, {"sku_code": "SKU-B", "quantity": 0}]  # fmt: skip


async def test_t8_maps_lead_times_and_booleans():
    out = await LT.get_operating_rules(_fake_engine(), None)

    assert out.version_no == 3 and out.sales_basis_days == 28
    assert out.lead_times.dest_customer_delivery_days == 2
    assert out.lead_times.donor_customer_delivery_days == 4
    assert out.lead_times.kr_air_transit_days == 3
    assert [(s.sku_code, s.emergency_air_eligible) for s in out.skus] == [
        ("SKU-A", True),
        ("SKU-B", False),
    ]


async def test_t8_business_day_row_becomes_null_as_contract_says():
    # 계약 9절 Q5. 엔진은 일수를 그대로 쓰므로 계산과 어긋난다(처리는 입력 검증 GLOW-100 에서 다시 정함)
    ctx = _fake_engine(
        air_kr=_lead("AIR_TRANSIT", W_KR, 3, "WEST", basis="BUSINESS_DAY")
    )
    out = await LT.get_operating_rules(ctx, None)

    assert out.lead_times.kr_air_transit_days is None
    assert out.lead_times.dest_outbound_handling_days == 1


async def test_t8_missing_row_is_null_and_row_engine_mismatch_stops():
    out = await LT.get_operating_rules(_fake_engine(oh_kr=None), None)
    assert out.lead_times.kr_outbound_handling_days is None

    ctx = _fake_engine()
    ctx.engine.snap["lt"]["cd_cross"] = (
        9  # 엔진 값과 고른 행의 일수가 다름 = 고르는 규칙이 엔진과 달라짐
    )
    with pytest.raises(ValueError, match="cd_cross"):
        await LT.get_operating_rules(ctx, None)


async def test_t8_rejects_non_boolean_text():
    ctx = _fake_engine()
    ctx.engine.snap["sku_attr"]["SKU-A"]["emergency_air_eligible"] = "t"
    with pytest.raises(ValueError, match="불리언"):
        await LT.get_operating_rules(ctx, None)


async def test_unknown_stage_fails_output_model():
    ctx = _fake_engine()
    ctx.engine.snap["stage"] = "LOST"
    with pytest.raises(ValidationError):
        await LT.get_delay_context(ctx, _t1_input(ctx))


async def test_tools_do_not_change_the_snapshot():
    ctx = _fake_engine()
    before = copy.deepcopy(ctx.engine.snap)
    for tool in (LT.get_inventory_snapshot, LT.get_inbound_schedule, LT.get_sales_history,
                 LT.get_open_orders, LT.get_operating_rules):  # fmt: skip
        first = await tool(ctx, None)
        assert await tool(ctx, None) == first  # 몇 번 불러도 같음
    await LT.get_delay_context(ctx, _t1_input(ctx))
    assert ctx.engine.snap == before


# --- 기준일 D (이슈 #51): resolve_base_date 단위 시험 --------------------------------------------
# 실행 날짜 01-10. 일 수집은 00:10Z 에 시작한다(그날 수집 = 전날 재고·전날 접수 주문)
OTHER = str(uuid.UUID(int=32))


class _FakeStore:
    """엔진 Store 중 resolve_base_date 가 읽는 것만(runs·inv·runs_until). runs_until 은 엔진 store.py 와 같은 식."""

    def __init__(self, runs, inv):
        self.runs = {r["data_sync_run_id"]: r for r in runs}
        self.inv = {(r["company_id"], r["sku_id"], r["inventory_date"], r["data_sync_run_id"]): r for r in inv}  # fmt: skip

    def runs_until(self, company_id, t):
        return [r for r in self.runs.values() if r["company_id"] == company_id and r["started_at"] <= t]  # fmt: skip


def _sync(run_id, domain, started_at, status="SUCCEEDED", company=COMPANY):
    return {"data_sync_run_id": run_id, "company_id": company, "source_domain": domain,
            "started_at": started_at, "status": status}  # fmt: skip


def _inv(run_id, day, company=COMPANY):
    return {
        "company_id": company,
        "sku_id": SKU_A,
        "inventory_date": day,
        "data_sync_run_id": run_id,
    }


def _resolve(runs, inv, confirm_t="2026-01-10T00:30:00Z"):
    return EI.resolve_base_date(_FakeStore(runs, inv), COMPANY, confirm_t)


def _basis(res):
    return res.base_date, res.late_sources


def test_base_date_is_run_date_when_both_sources_are_in():
    # ① 그날 재고(01-09분)·주문(01-09 접수분) 수집이 끝난 뒤 = 실행 날짜
    runs = [_sync("i9", "INVENTORY", "2026-01-09T00:10:00Z"), _sync("i10", "INVENTORY", "2026-01-10T00:10:00Z"),
            _sync("o9", "ORDER", "2026-01-09T00:10:00Z"), _sync("o10", "ORDER", "2026-01-10T00:10:00Z")]  # fmt: skip
    res = _resolve(runs, [_inv("i9", "2026-01-08"), _inv("i10", "2026-01-09")])
    assert res == EI.BaseDateResolution(
        base_date=date(2026, 1, 10), run_date=date(2026, 1, 10),
        inventory_limit=date(2026, 1, 10), order_limit=date(2026, 1, 10), late_sources=(),
    )  # fmt: skip


def test_base_date_moves_back_when_todays_collections_have_not_started():
    # ② 00:05 실행: 그날 수집(00:10)은 아직 시작 전 → 두 원천 모두 하루 늦음, D = 전날
    runs = [_sync("i9", "INVENTORY", "2026-01-09T00:10:00Z"), _sync("i10", "INVENTORY", "2026-01-10T00:10:00Z"),
            _sync("o9", "ORDER", "2026-01-09T00:10:00Z"), _sync("o10", "ORDER", "2026-01-10T00:10:00Z")]  # fmt: skip
    res = _resolve(
        runs,
        [_inv("i9", "2026-01-08"), _inv("i10", "2026-01-09")],
        "2026-01-10T00:05:00Z",
    )
    assert _basis(res) == (date(2026, 1, 9), ("INVENTORY", "ORDER"))
    assert res.run_date == date(2026, 1, 10)


def test_only_order_late():
    # ③ 재고는 들어왔고 주문 수집만 늦음
    runs = [
        _sync("i10", "INVENTORY", "2026-01-10T00:10:00Z"),
        _sync("o9", "ORDER", "2026-01-09T00:10:00Z"),
    ]
    res = _resolve(runs, [_inv("i10", "2026-01-09")])
    assert _basis(res) == (date(2026, 1, 9), ("ORDER",))
    assert (res.inventory_limit, res.order_limit) == (
        date(2026, 1, 10),
        date(2026, 1, 9),
    )


def test_only_inventory_late():
    # ④ 주문은 들어왔고 재고만 늦음(마지막 재고일 01-08)
    runs = [
        _sync("i9", "INVENTORY", "2026-01-09T00:10:00Z"),
        _sync("o10", "ORDER", "2026-01-10T00:10:00Z"),
    ]
    res = _resolve(runs, [_inv("i9", "2026-01-08")])
    assert _basis(res) == (date(2026, 1, 9), ("INVENTORY",))


def test_run_date_caps_base_date():
    # ⑤ 한도가 실행 날짜보다 늦어도(미래 재고일 행, ORDER 수집 없음) D 는 실행 날짜를 넘지 않는다
    runs = [_sync("i10", "INVENTORY", "2026-01-10T00:10:00Z")]
    res = _resolve(runs, [_inv("i10", "2026-01-12")])
    assert _basis(res) == (date(2026, 1, 10), ())
    assert (res.inventory_limit, res.order_limit) == (date(2026, 1, 13), None)


def test_failed_or_running_order_collection_does_not_count():
    # ⑥ 그날 ORDER 수집이 FAILED·RUNNING 이면 그 전 성공(PARTIAL_SUCCESS 포함) 수집 기준. 엔진 order_collection_ok 와 같음
    runs = [_sync("i10", "INVENTORY", "2026-01-10T00:10:00Z"),
            _sync("o9", "ORDER", "2026-01-09T00:10:00Z", "PARTIAL_SUCCESS"),
            _sync("o10", "ORDER", "2026-01-10T00:10:00Z", "FAILED"),
            _sync("o10b", "ORDER", "2026-01-10T00:20:00Z", "RUNNING")]  # fmt: skip
    res = _resolve(runs, [_inv("i10", "2026-01-09")])
    assert _basis(res) == (date(2026, 1, 9), ("ORDER",))


def test_initial_load_covers_all_of_2025():
    # ⑦ 2026-01-01 전에 시작한 초기 적재는 2025 접수일 전부를 다룸 → ORDER 한도 2026-01-01(시작 날짜 + 0 이 아님)
    runs = [
        _sync("init", "ORDER", "2025-12-15T00:00:00Z"),
        _sync("i10", "INVENTORY", "2026-01-10T00:10:00Z"),
    ]
    res = _resolve(runs, [_inv("i10", "2026-01-09")])
    assert res.order_limit == date(2026, 1, 1)
    assert _basis(res) == (date(2026, 1, 1), ("ORDER",))


def test_rows_after_confirm_time_or_of_other_company_do_not_count():
    # ⑧ confirm_t 뒤에 시작한 수집의 재고 행은 엔진 inventory_row 처럼 안 셈. 다른 고객사 수집·재고도 안 셈
    runs = [_sync("i9", "INVENTORY", "2026-01-09T00:10:00Z"), _sync("i10", "INVENTORY", "2026-01-10T00:40:00Z"),
            _sync("o10", "ORDER", "2026-01-10T00:10:00Z"),
            _sync("x10", "INVENTORY", "2026-01-10T00:10:00Z", company=OTHER),
            _sync("xo", "ORDER", "2026-01-10T00:10:00Z", company=OTHER)]  # fmt: skip
    inv = [
        _inv("i9", "2026-01-08"),
        _inv("i10", "2026-01-09"),
        _inv("x10", "2026-01-09", company=OTHER),
    ]
    res = _resolve(runs, inv)
    assert _basis(res) == (date(2026, 1, 9), ("INVENTORY",))
    assert res.order_limit == date(2026, 1, 10)


def test_no_data_at_all_keeps_run_date_for_engine_hold():
    # ⑨ 재고 행·성공 ORDER 수집이 하나도 없으면 한도 없음 → D = 실행 날짜, 보류는 엔진 메시지에 맡김
    res = _resolve([_sync("o10", "ORDER", "2026-01-10T00:10:00Z", "FAILED")], [])
    assert res == EI.BaseDateResolution(
        base_date=date(2026, 1, 10), run_date=date(2026, 1, 10),
        inventory_limit=None, order_limit=None, late_sources=(),
    )  # fmt: skip


def _run_input(stage, expected):
    return EI._RunInput(
        shipment_id=uuid.UUID(int=42), confirm_t="2026-01-10T00:10:00Z", run_date=date(2026, 1, 10),
        planned=date(2026, 1, 12), stage=stage, expected=expected,
    )  # fmt: skip


def test_build_passes_trigger_stage_and_expected_date_to_engine(monkeypatch):
    # overlay 21건은 트리거 스냅샷 = 입력 확정 시각의 최신 관측이라, 트리거 값을 넘기지 않아도 정답 대조가 통과한다
    # (11-06 변이 시험에서 확인). 그래서 엔진에 넘기는 값을 직접 본다. 엔진 하네스는 AUTO 실행에 트리거 값을 넘긴다
    # ⑩ 엔진에 넘기는 D 는 실행 날짜가 아니라 resolve_base_date 의 D 다(#51)
    basis = EI.BaseDateResolution(
        base_date=date(2026, 1, 9), run_date=date(2026, 1, 10),
        inventory_limit=date(2026, 1, 9), order_limit=date(2026, 1, 10), late_sources=("INVENTORY",),
    )  # fmt: skip
    resolved = []
    monkeypatch.setattr(
        EI,
        "resolve_base_date",
        lambda store, cid, t: resolved.append((store, cid, t)) or basis,
    )
    calls = []

    class Hold(Exception):
        def __init__(self, code, message):
            super().__init__(message)
            self.code, self.message = code, message

    def build(store, cal, shipment_id, day, confirm_t, stage=None, E=None):
        calls.append((shipment_id, day, confirm_t, stage, E))
        if stage == "AT_PORT":
            raise Hold("REQUIRED_DATA_INVALID", "재고 행 없음")
        return {"snap": 1}

    modules = {
        "snapshot": SimpleNamespace(build=build, Hold=Hold),
        "pipeline": SimpleNamespace(analyze=lambda snap: {"analysis": snap}),
    }
    monkeypatch.setattr(EI, "load_engine_module", modules.__getitem__)
    monkeypatch.setattr(EI, "_calendar", lambda: "CAL")

    assert EI._build_and_analyze("STORE", COMPANY, _run_input("AT_SEA", date(2026, 1, 20))) == (
        {"snap": 1}, {"analysis": {"snap": 1}}, basis)  # fmt: skip
    EI._build_and_analyze(
        "STORE", COMPANY, _run_input(None, None)
    )  # 재분석: 엔진이 최신 관측을 고름
    hold = EI._build_and_analyze(
        "STORE", COMPANY, _run_input("AT_PORT", date(2026, 1, 20))
    )

    assert calls[0] == (
        str(uuid.UUID(int=42)),
        date(2026, 1, 9),  # 실행 날짜 01-10 이 아님
        "2026-01-10T00:10:00Z",
        "AT_SEA",
        "2026-01-20",
    )
    assert resolved[0] == ("STORE", COMPANY, "2026-01-10T00:10:00Z")
    assert calls[1][3:] == (None, None)
    assert hold == EI.EngineHold(code="REQUIRED_DATA_INVALID", message="재고 행 없음")


async def test_run_not_visible_tells_to_check_worker_session_and_active_company(
    monkeypatch,
):
    # 앱 역할 세션이나 ACTIVE 가 아닌 고객사는 RLS 로 0행이 된다. 원인을 메시지에서 바로 알 수 있어야 한다(PR #53 리뷰)
    async def no_rls(session, *, user_id, company_id):
        pass

    async def execute(sql, params):
        return SimpleNamespace(one_or_none=lambda: None)

    monkeypatch.setattr(EI, "set_rls_context", no_rls)
    run = SimpleNamespace(
        analysis_run_id=uuid.UUID(int=1), company_id=uuid.UUID(int=2),
        delay_event_id=uuid.UUID(int=3), trigger_shipment_schedule_snapshot_id=None,
    )  # fmt: skip
    with pytest.raises(LookupError, match="워커 역할 세션인지, 고객사가 ACTIVE 인지"):
        await EI._run_input(SimpleNamespace(execute=execute), run)


async def test_no_delay_uses_trigger_expected_date_without_query():
    session = (
        SimpleNamespace()
    )  # 조회하면 AttributeError: 트리거가 있으면 최신 관측을 읽지 않는다
    ri = _run_input("AT_SEA", date(2026, 1, 20))
    assert await EI._expected_for_no_delay(session, SimpleNamespace(), ri) == date(
        2026, 1, 20
    )


def test_decision_of_returns_copies():
    analysis = {"status": "NO_RISK", "result_class": "PURE_NO_RISK", "scope": {"a": [1]},
                "signature": {"schema_version": "decision_signature.v1"}, "sha": "c" * 64}  # fmt: skip
    decision = EI.decision_of(SimpleNamespace(analysis=analysis))
    decision.dependency_scope["a"].append(2)
    decision.decision_signature["x"] = 1

    assert analysis["scope"] == {"a": [1]} and "x" not in analysis["signature"]
    assert decision.decision_signature_sha256 == "c" * 64


# --- 실제 엔진 + 개발 DB -----------------------------------------------------------------------


@pytest.fixture
def engine_dir():
    if get_settings().glowship_engine_dir is None:
        pytest.skip("GLOWSHIP_ENGINE_DIR 이 없어 실제 엔진 시험을 건너뜀")


async def _runs(session, company_code: str) -> list[AnalysisRun]:
    from app.modules.company.infrastructure.company import Company

    company_id = await session.scalar(
        select(Company.company_id).where(Company.company_code == company_code)
    )
    if company_id is None:
        pytest.skip(f"고객사 {company_code} 가 없는 DB(overlay 미적재)라 건너뜀")
    rows = await session.scalars(
        select(AnalysisRun)
        .where(AnalysisRun.company_id == company_id)
        .order_by(AnalysisRun.started_at, AnalysisRun.run_code)
    )
    return list(rows)


def _by_code(runs, code):
    found = [r for r in runs if r.run_code == code]
    if not found:
        pytest.skip(f"실행 {code} 가 없는 DB 라 건너뜀")
    return found[0]


async def _check_run(session, run: AnalysisRun) -> str:
    """실행 하나를 엔진 하네스 순서로 다시 만들고 저장된 값과 대조한다. 돌려주는 값 = 거친 길."""
    no_delay = await EI.judge_no_delay(session, run)
    if run.completed_status == "NO_DELAY":
        assert no_delay is not None, run.run_code
        assert no_delay.decision_signature_sha256 == run.decision_signature_sha256
        assert no_delay.decision_signature == run.decision_signature
        return "NO_DELAY"
    assert no_delay is None, run.run_code

    built = await EI.build_engine_input(session, run)
    if run.completed_status == "ON_HOLD":
        assert built == EI.EngineHold(code=run.failure_code, message=built.message), (
            run.run_code
        )
        return "ON_HOLD"
    assert isinstance(built, EI.EngineInput), (run.run_code, built)
    # ⑪ overlay 실행은 모두 그날 수집이 입력에 들어온 뒤에 만들어져 새 규칙(#51)으로도 D = 실행 날짜, 늦은 원천 없음
    run_date = run.started_at.astimezone(UTC).date()
    basis = built.base_date_resolution
    assert (built.base_date, basis.run_date, basis.late_sources) == (
        run_date,
        run_date,
        (),
    ), run.run_code
    assert built.snap["D"] == built.base_date == basis.base_date, run.run_code
    decision = EI.decision_of(built)
    if (
        run.decision_signature_sha256 is not None
    ):  # 시간 초과 주입 실행(RT-1)은 서명을 지움
        assert decision.decision_signature_sha256 == run.decision_signature_sha256, (
            run.run_code
        )
        assert decision.decision_signature == run.decision_signature, run.run_code
    if run.dependency_scope is not None:  # FAILED 는 의존 범위를 비움
        assert decision.dependency_scope == run.dependency_scope, run.run_code
    if run.completed_status in ("READY_FOR_REVIEW", "NO_RISK"):
        assert (decision.status, decision.result_class) == (
            run.completed_status,
            run.result_class,
        )
    await _check_tools(built, run)
    return decision.status


async def _check_tools(engine: EI.EngineInput, run: AnalysisRun) -> None:
    ctx = SimpleNamespace(engine=engine)
    t1 = await LT.get_delay_context(
        ctx,
        GetDelayContextInput(
            analysis_run_id=str(run.analysis_run_id), delay_event_id=str(run.delay_event_id),
            base_date=engine.base_date, input_seq=run.input_seq,
        ),
    )  # fmt: skip
    t2 = await LT.get_inventory_snapshot(ctx, None)
    t3 = await LT.get_inbound_schedule(ctx, None)
    t4 = await LT.get_sales_history(ctx, None)
    t5 = await LT.get_open_orders(ctx, None)
    t8 = await LT.get_operating_rules(ctx, None)
    snap, analysis = engine.snap, engine.analysis

    if run.trigger_shipment_schedule_snapshot_id is not None:
        assert t1.trigger_shipment_schedule_snapshot_id == str(
            run.trigger_shipment_schedule_snapshot_id
        )
    assert t8.operating_rule_set_id == str(run.operating_rule_set_id)
    # T4 합계 ÷ 28 = 엔진 평균(mu_ch). 정수 합계를 다른 색인에서 세도 엔진 계산과 같은 수요다
    wh_id = {
        w["warehouse_code"]: w["warehouse_id"] for w in (snap["dest"], snap["donor"])
    }
    for t in t4.demand_totals:
        assert (
            t.quantity / 28
            == snap["mu_ch"][(wh_id[t.warehouse_code], t.sku_code, t.sales_channel)]
        )
    # 개발 3 요청: T8 skus[] 가 영향·관찰·선적 SKU 를 모두 담는다
    t8_codes = {s.sku_code for s in t8.skus}
    assert (
        set(analysis.get("k_imp") or [])
        | set(analysis.get("k_watch") or [])
        | set(snap["k_shipment"])
        <= t8_codes
    )
    assert {i.sku_code for w in t2.warehouses for i in w.items} == t8_codes
    # T5 주문 = 의존 범위의 약속 주문 줄을 모두 포함
    scope = analysis.get("scope")
    if scope:
        t5_lines = {ln.order_line_id for o in t5.orders for ln in o.lines}
        assert set(scope["entities"]["committed_order_line_ids"]) <= t5_lines
        assert set(scope["entities"]["inbound_shipment_ids"]) <= {
            x.shipment_id for x in t3.inbound
        }


async def test_no_delay_and_hold_runs_match_stored_results(db_session, engine_dir):
    runs = await _runs(db_session, "B_COSMETICS")
    assert await _check_run(db_session, _by_code(runs, "AN-260102-001")) == "NO_DELAY"
    assert await _check_run(db_session, _by_code(runs, "AN-260102-002")) == "ON_HOLD"


async def test_ready_run_rebuilds_stored_decision_and_tool_outputs(
    db_session, engine_dir
):
    runs = await _runs(db_session, "A_BEAUTY")
    assert (
        await _check_run(db_session, _by_code(runs, "AN-260101-002"))
        == "READY_FOR_REVIEW"
    )


async def test_reanalysis_without_trigger_uses_latest_observation(
    db_session, engine_dir
):
    # 재분석은 트리거 스냅샷이 없다. 엔진처럼 입력 확정 시각까지의 최신 관측(snapshot_at)으로 E 를 정한다
    runs = await _runs(db_session, "A_BEAUTY")
    run = _by_code(runs, "AN-260102-001")
    assert run.trigger_shipment_schedule_snapshot_id is None
    assert await _check_run(db_session, run) == "READY_FOR_REVIEW"


async def test_run_before_next_days_collections_uses_last_data_day(
    db_session, engine_dir
):
    # ⑫ 데모 상황 재현(#51, PR #56 검증 PV2-6): 같은 실행을 다음 날 00:05Z 에 만든 것으로 바꾼다.
    #    입력 순번·트리거는 그대로라 다음 날 수집(00:02Z 시작)은 입력에 없고 엔진이 보는 자료는 같다(= 그날 자료가 입력에 들어오기 전 실행). 새 규칙은 D 를 마지막 자료 날(원래 D)로 당겨 보류 없이
    #    같은 판정 서명을 내고, 옛 규칙(D = 실행 날짜)은 엔진이 보류한다(음성 대조)
    runs = await _runs(db_session, "A_BEAUTY")
    run = _by_code(runs, "AN-260101-002")
    run_day = run.started_at.astimezone(UTC).date()
    later = SimpleNamespace(
        analysis_run_id=run.analysis_run_id, company_id=run.company_id, delay_event_id=run.delay_event_id,
        input_seq=run.input_seq, trigger_shipment_schedule_snapshot_id=run.trigger_shipment_schedule_snapshot_id,
        started_at=datetime.combine(run_day + timedelta(days=1), time(0, 5), tzinfo=UTC),
    )  # fmt: skip

    built = await EI.build_engine_input(db_session, later)

    assert isinstance(built, EI.EngineInput), built
    basis = built.base_date_resolution
    assert (basis.run_date, basis.base_date, basis.late_sources) == (
        run_day + timedelta(days=1), run_day, ("INVENTORY", "ORDER"))  # fmt: skip
    assert built.base_date == built.snap["D"] == run_day
    decision = EI.decision_of(built)
    assert decision.decision_signature_sha256 == run.decision_signature_sha256
    assert decision.dependency_scope == run.dependency_scope

    snapshot = EI.load_engine_module("snapshot")
    with pytest.raises(snapshot.Hold, match="ORDER 수집 결측일"):
        snapshot.build(
            built.store, EI._calendar(), str(built.shipment_id), basis.run_date, built.confirm_t,
            stage=built.snap["stage"], E=built.snap["E"].isoformat(),
        )  # fmt: skip


# --- 정답 대조: overlay 실행 21건 (선택 실행) ---------------------------------------------------


@pytest.mark.parametrize("company_code", ["A_BEAUTY", "B_COSMETICS"])
async def test_all_overlay_runs_match_stored_results_golden(
    db_session, engine_dir, company_code
):
    if os.environ.get("GLOWSHIP_ENGINE_GOLDEN") != "1":
        pytest.skip("GLOWSHIP_ENGINE_GOLDEN=1 일 때만 도는 정답 대조(몇 분)라 건너뜀")
    runs = await _runs(db_session, company_code)
    paths = [await _check_run(db_session, run) for run in runs]
    assert len(paths) == {"A_BEAUTY": 13, "B_COSMETICS": 8}[company_code]
