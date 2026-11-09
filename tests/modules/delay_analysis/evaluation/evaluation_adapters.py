import uuid
from datetime import timedelta
from types import SimpleNamespace

from app.modules.delay_analysis.application.agent.tools import impact_tools as IT
from app.modules.delay_analysis.application.agent.tools.impact_schemas import (
    CalculateInventoryImpactInput,
    IdentifyAtRiskOrdersInput,
)
from app.modules.delay_analysis.application.agent.tools.lookup_schemas import (
    DelayContext,
    DonorNextInbound,
    InboundSchedule,
    InventorySnapshot,
    KrOnHand,
    LeadTimes,
    OpenOrders,
    OperatingRules,
    SalesHistory,
    SkuAttribute,
)
from app.modules.delay_analysis.application.agent.tools.option_feasibility import (
    check_option_feasibility,
)
from app.modules.delay_analysis.application.agent.tools.option_schemas import (
    CalculateOptionOutcomesInput,
    CheckOptionFeasibilityInput,
    CompareOptionsInput,
    OptionComparisonInput,
    OptionFeasibility,
    OptionOutcomes,
    OptionRanking,
)
from app.modules.delay_analysis.infrastructure.engine.engine_loader import (
    load_engine_module,
)

"""
    엔진 snap·analysis → 서버 도구 입력 (GLOW-58 1단계, 시험 전용)
      - T9 입력: 엔진 snap 의 같은 뜻 키를 계약 이름(tool-contracts.md 5절 입력 표)으로 옮긴다.
      - T11 입력: 엔진 analysis["options"] 에서 순위에 쓰는 값만 뽑는다(7절).
      - T10 입력(outcomes_input): 시나리오 snap 에는 엔진 Store 가 없어 조회 도구 T1~T5·T8 을 부를 수 없으므로 같은 뜻 키를
        계약 모델로 옮긴다. T7·T6 은 실제 도구(impact_tools)를, T9 는 실제 T9 를 부른다.
        시나리오에 없는 칸(창고 권역·트리거 스냅샷·규칙 판·행사 일정·재생 해시)은 계산에 쓰지 않는 자리 값으로 채운다.
      - 엔진 대응안 → T10 출력(engine_outcomes): storage.py 112~134행·pipeline.aggregates 와 같은 변환.
        엔진 options._rank 의 3번째 정렬 키 supply_date 가 계약의 expected_delivery_date 자리에 간다.
      - 운영 코드는 이 어댑터를 쓰지 않는다. 같은 값은 엔진 입력 저장소(GLOW-125)와 T1~T8 이 만든다.
      - 행 모양을 정답표와 바로 견주도록 *_rows 가 enum·date 를 문자열로 편다.
"""

# 계약 이름 → 엔진 snap["lt"] 키
LEAD_TIME_KEYS = {
    "dest_outbound_handling_days": "oh_dest",
    "dest_customer_delivery_days": "cd_same",
    "donor_outbound_handling_days": "oh_donor",
    "donor_customer_delivery_days": "cd_cross",
    "kr_outbound_handling_days": "oh_kr",
    "kr_air_transit_days": "air_kr",
    "dest_inbound_handling_days": "inbound_dest",
}


def feasibility_input(snap: dict, analysis: dict) -> CheckOptionFeasibilityInput:
    """T9 입력. SKU 행은 snap 의 모든 SKU(sku_code 정렬), 영향 SKU 는 엔진 k_imp."""
    codes = sorted(snap["sku_ids"])
    return CheckOptionFeasibilityInput(
        transport_mode=snap["mode"],
        stage=snap["stage"],
        base_date=snap["D"],
        expected_3pl_inbound_date=snap["E"],
        planned_kr_release_date=snap["planned_kr_release"],
        lead_times=LeadTimes(
            **{name: snap["lt"][key] for name, key in LEAD_TIME_KEYS.items()}
        ),
        impacted_sku_codes=sorted(analysis["k_imp"]),
        skus=[
            SkuAttribute(
                sku_id=snap["sku_ids"][code],
                sku_code=code,
                # 엔진은 문자열 "true"/"false" 로 들고 "true" 와만 견준다(options.py)
                emergency_air_eligible=snap["sku_attr"][code]["emergency_air_eligible"]
                == "true",
            )
            for code in codes
        ],
        kr_on_hand=[
            KrOnHand(sku_code=code, on_hand_quantity=snap["kr_on_hand"][code])
            for code in codes
            if code in snap["kr_on_hand"]
        ],
        # 엔진 next_inbound 는 다음 입고가 없는 SKU 를 키에서 뺀다 → 계약은 그 행을 null 로 둔다(R-03)
        donor_next_inbound=[
            DonorNextInbound(
                sku_code=code, next_inbound_date=snap["next_inbound"].get(code)
            )
            for code in codes
        ],
    )


def comparison_input(analysis: dict) -> CompareOptionsInput:
    """T11 입력. 엔진 대응안 행 순서(고정 순서)를 그대로 둔다."""
    return CompareOptionsInput(
        options=[
            OptionComparisonInput(
                option_type=o["option_type"],
                feasibility_status=o["status"],
                delay_induced_recovered_orders=o.get("delay_induced_recovered"),
                reduction_committed_total=o.get("committed_total"),
                expected_delivery_date=o.get("supply_date"),
            )
            for o in analysis["options"]
        ]
    )


def precheck_rows(result: OptionFeasibility) -> dict[str, dict]:
    """대응안 → 사전 판정 행(정답표 모양). dict 는 입력 순서를 지킨다."""
    return {
        o.option_type.value: {
            "precheck": o.precheck.value,
            "reason_code": None if o.reason_code is None else o.reason_code.value,
            "supply_available_date": (
                None
                if o.supply_available_date is None
                else o.supply_available_date.isoformat()
            ),
            "eligible_sku_codes": list(o.eligible_sku_codes),
        }
        for o in result.options
    }


def ranking_rows(result: OptionRanking) -> dict[str, dict]:
    return {
        o.option_type.value: {
            "rank": o.recommendation_rank,
            "recommended": o.is_recommended,
        }
        for o in result.options
    }


def engine_ranking_rows(analysis: dict) -> dict[str, dict]:
    """엔진 options.py _rank 가 적은 rank·recommended 를 같은 모양으로."""
    return {
        o["option_type"]: {"rank": o["rank"], "recommended": o["recommended"]}
        for o in analysis["options"]
    }


def engine_supply_dates(analysis: dict) -> dict[str, str | None]:
    return {
        o["option_type"]: None
        if o["supply_date"] is None
        else o["supply_date"].isoformat()
        for o in analysis["options"]
    }


# --- T10 ---------------------------------------------------------------------------------------------

_CHANNELS = ("AMAZON", "SEPHORA")
_PLACEHOLDER_ID = str(uuid.UUID(int=0))
_PLACEHOLDER_SHA = "0" * 64
_REGION = {"dest": "WEST", "donor": "EAST", "kr": "KR"}


def _warehouse(snap: dict, key: str) -> dict:
    w = snap[key]
    return {
        "warehouse_id": w["warehouse_id"],
        "warehouse_code": w["warehouse_code"],
        "region_code": w.get("region_code", _REGION[key]),
    }


def _demand_total(mu: float) -> int:
    """엔진 mu = 28일 합계 ÷ 28. 시나리오는 mu 를 바로 적으므로 합계로 되돌리고, 되돌린 값이 같은 mu 인지 본다"""
    total = round(mu * 28)
    assert total / 28 == mu, f"28일 합계로 나타낼 수 없는 mu {mu}"
    return total


def _delay_context(snap: dict) -> DelayContext:
    return DelayContext.model_validate({
        "shipment_id": snap["delayed_shipment_id"], "base_date": snap["D"],
        "planned_3pl_inbound_date": snap["P"], "expected_3pl_inbound_date": snap["E"],
        "stage": snap["stage"], "transport_mode": snap["mode"], "planned_kr_release_date": snap["planned_kr_release"],
        "trigger_shipment_schedule_snapshot_id": snap.get("trigger_snapshot_id") or _PLACEHOLDER_ID,
        "shipment_skus": [
            {"sku_id": snap["sku_ids"][c], "sku_code": c, "delayed_quantity": snap["delayed_qty"].get(c, 0)}
            for c in sorted(snap["k_shipment"])
        ],
        "destination_warehouse": _warehouse(snap, "dest"), "donor_warehouse": _warehouse(snap, "donor"),
        "kr_origin_warehouse": _warehouse(snap, "kr"),
    })  # fmt: skip


def _us(snap: dict) -> list[dict]:
    return sorted((snap["dest"], snap["donor"]), key=lambda w: w["warehouse_code"])


def _inventory(snap: dict, codes: list[str]) -> InventorySnapshot:
    return InventorySnapshot.model_validate({
        "inventory_date": snap["D"] - timedelta(1),
        "warehouses": [
            {"warehouse_code": w["warehouse_code"], "items": [
                {"sku_code": c, "on_hand_quantity": snap["restore"][w["warehouse_id"]]["on_hand"].get(c, 0),
                 "reserved_quantity": snap["restore"][w["warehouse_id"]]["reserved"].get(c, 0)} for c in codes]}
            for w in _us(snap)
        ],
        "kr_on_hand": [{"sku_code": c, "on_hand_quantity": q} for c, q in sorted(snap["kr_on_hand"].items())],
    })  # fmt: skip


def _inbound(snap: dict, codes: list[str]) -> InboundSchedule:
    rows = [
        {"shipment_id": x["shipment_id"], "warehouse_id": w["warehouse_id"], "warehouse_code": w["warehouse_code"],
         "expected_3pl_inbound_date": x["raw_expected"], "arrival_date": x["arrival"],
         "quantities": [{"sku_code": c, "quantity": q} for c, q in sorted(x["qty"].items())]}
        for w in _us(snap) for x in snap["inbound"][w["warehouse_id"]]
    ]  # fmt: skip
    return InboundSchedule.model_validate({
        "inbound": sorted(rows, key=lambda r: (r["warehouse_code"], r["arrival_date"], r["shipment_id"])),
        "donor_next_inbound": [{"sku_code": c, "next_inbound_date": snap["next_inbound"].get(c)} for c in codes],
    })  # fmt: skip


def _sales(snap: dict, codes: list[str]) -> SalesHistory:
    code_of_wh = {w["warehouse_id"]: w["warehouse_code"] for w in _us(snap)}
    for w in _us(snap):
        for c in codes:
            by_channel = sum(
                _demand_total(snap["mu_ch"][(w["warehouse_id"], c, ch)])
                for ch in _CHANNELS
            )
            assert by_channel / 28 == snap["mu"][(w["warehouse_id"], c)], (
                "채널 합계와 창고 합계가 다른 시나리오"
            )
    basis = snap["sales_basis"]
    return SalesHistory.model_validate({
        "sales_basis_type": "ORDER_DEMAND", "basis_start_date": basis["start"], "basis_end_date": basis["end"],
        "basis_days": 28,
        "demand_totals": [
            {"warehouse_code": w["warehouse_code"], "sku_code": c, "sales_channel": ch,
             "quantity": _demand_total(snap["mu_ch"][(w["warehouse_id"], c, ch)])}
            for w in _us(snap) for c in codes for ch in _CHANNELS
        ],
        "event_days": basis["event_days"], "event_names": list(basis["event_names"]),
        "calendar_version": basis.get("calendar_version", "fixture"),
        "calendar_sha256": basis.get("calendar_sha256", _PLACEHOLDER_SHA),
        "actual_known_days": list(snap["actual_known_days"]),
        "actual_demand": sorted(
            ({"warehouse_code": code_of_wh[w], "sku_code": c, "demand_date": day, "quantity": q}
             for (w, c, day), q in snap["actual_demand"].items()),
            key=lambda r: (r["warehouse_code"], r["sku_code"], r["demand_date"])),
    })  # fmt: skip


def _open_orders(snap: dict) -> OpenOrders:
    code_of = snap["sku_code_by_id"]
    orders = [
        {"order_id": o["order_id"], "order_code": o["order_code"], "warehouse_id": o["warehouse_id"],
         "sales_channel": o["sales_channel"], "ordered_at": o["ordered_at"], "promised_delivery_at": o["promised_delivery_at"],
         "lines": [
             {"order_line_id": ln["order_line_id"], "line_no": int(ln["line_no"]), "sku_id": ln["sku_id"],
              "sku_code": code_of[ln["sku_id"]], "ordered_quantity": int(ln["ordered_quantity"]), "line_status": ln["line_status"]}
             for ln in sorted(snap["lines_by_order"][o["order_id"]], key=lambda ln: int(ln["line_no"]))]}
        for w in _us(snap) for o in snap["restore"][w["warehouse_id"]]["open_orders"]
    ]  # fmt: skip
    state = []
    for w in _us(snap):
        rest = snap["restore"][w["warehouse_id"]]
        state.append({
            "warehouse_code": w["warehouse_code"],
            "replay_result_sha256": rest.get("result_sha256", _PLACEHOLDER_SHA),
            "pending_dispatch": [
                {"order_code": p["order_code"], "secured_date": p["secured_date"],
                 "lines": [{"line_no": ln["line_no"], "sku_code": ln["sku_code"], "quantity": ln["quantity"]} for ln in p["lines"]]}
                for p in sorted(rest["carry"], key=lambda p: p["order_code"])],
            "waiting_order_codes": sorted(o["order_code"] for o in rest["waiting"]),
            "blocked_order_protection": [{"sku_code": c, "quantity": q} for c, q in sorted(rest.get("protection", {}).items())],
            "secured_waiting_for_dispatch": [{"sku_code": c, "quantity": q} for c, q in sorted(rest.get("secured_wait", {}).items())],
        })  # fmt: skip
    return OpenOrders.model_validate(
        {
            "orders": sorted(orders, key=lambda o: o["order_code"]),
            "allocation_state": state,
        }
    )


def _rules(snap: dict, codes: list[str]) -> OperatingRules:
    rule = snap["rule"]
    return OperatingRules.model_validate({
        "operating_rule_set_id": rule["operating_rule_set_id"], "version_no": int(rule.get("version_no", 1)),
        "safety_stock_days": snap["safety_days"], "sales_basis_days": snap["basis_days"],
        "lead_times": {name: snap["lt"][key] for name, key in LEAD_TIME_KEYS.items()},
        "skus": [{"sku_id": snap["sku_ids"][c], "sku_code": c,
                  "emergency_air_eligible": snap["sku_attr"][c]["emergency_air_eligible"] == "true"} for c in codes],
    })  # fmt: skip


async def outcomes_input(snap: dict, analysis: dict) -> CalculateOptionOutcomesInput:
    """T10 입력: T1~T5·T8 은 snap 에서 옮기고, T7·T6·T9 는 실제 도구를 그래프 순서로 부른다"""
    codes = sorted(snap["sku_ids"])
    t1, t2, t3 = _delay_context(snap), _inventory(snap, codes), _inbound(snap, codes)
    t4, t5, t8 = _sales(snap, codes), _open_orders(snap), _rules(snap, codes)
    ctx = SimpleNamespace(engine=SimpleNamespace(snap=snap, analysis=analysis))
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
    return CalculateOptionOutcomesInput(
        option_feasibility=t9, delay_context=t1, inventory_snapshot=t2, inbound_schedule=t3,
        sales_history=t4, open_orders=t5, operating_rules=t8, inventory_impact=t6, at_risk_orders=t7)  # fmt: skip


def engine_outcomes(analysis: dict) -> OptionOutcomes:
    """엔진 analysis["options"] → 계약 6절 출력 (storage.py 112~134행·pipeline.aggregates 와 같은 변환)"""
    pipeline = load_engine_module("pipeline")
    options = []
    for o in analysis["options"]:
        ag = pipeline.aggregates(o)
        rows = []
        for x in o["outcomes"] or []:
            for ln in x["risk"]["lines"]:
                sec = x["secured"].get(ln["order_line_id"], 0)
                rows.append({
                    "order_line_id": ln["order_line_id"],
                    "outcome": "RECOVERED" if x["recovered"] else "NOT_RECOVERED",
                    "not_recovered_reason": None if x["recovered"] else x.get("reason"),
                    "line_securement_status": "FULLY_SECURED" if sec >= ln["open_quantity"]
                    else "PARTIALLY_SECURED" if sec > 0 else "NOT_SECURED",
                    "secured_quantity": sec,
                    "recovered_quantity": ln["open_quantity"] if x["recovered"] else 0,
                    "expected_delivery_at": f"{x['delivery'].isoformat()}T23:59:59Z"
                    if x["recovered"] and x["delivery"] else None,
                    "source_warehouse_id": x["src_wh"] if sec > 0 else None,
                    "supply_source": x["source"] if sec > 0 else None,
                })  # fmt: skip
        options.append({
            "option_type": o["option_type"], "feasibility_status": o["status"], "reason_code": o["reason_code"],
            "required_quantity": o["required"],
            "securable_quantity": o["supply_total"] if o["outcomes"] is not None else None,
            "reduction_committed_total": ag["committed"], "expected_delivery_date": o["supply_date"],
            "fully_recovered_orders": ag["fully"], "delay_induced_recovered_orders": ag["delay_rec"],
            "item_secured_blocked_orders": ag["blocked"], "not_recovered_orders": ag["not_rec"],
            "sku_results": {"schema_version": "sku_results.v1", "calculation_contract": "T14-v1", "unit": "EA",
                            "items": sorted(o["items"] or [], key=lambda x: x["sku_id"])},
            "outcomes": sorted(rows, key=lambda x: x["order_line_id"]),
        })  # fmt: skip
    return OptionOutcomes.model_validate({"options": options})
