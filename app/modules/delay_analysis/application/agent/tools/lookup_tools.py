from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from app.modules.delay_analysis.application.agent.tools.lookup_schemas import (
    DelayContext,
    GetDelayContextInput,
    InboundSchedule,
    InventorySnapshot,
    OpenOrders,
    OperatingRules,
    SalesHistory,
)
from app.modules.delay_analysis.infrastructure.analysis_enums import SalesBasisType
from app.modules.delay_analysis.infrastructure.engine.engine_operating_rules import (
    SUPPORTED_DAY_BASIS,
    SUPPORTED_SALES_BASIS_DAYS,
    lead_time_rows,
)

if TYPE_CHECKING:
    from app.modules.delay_analysis.infrastructure.engine.engine_input import (
        EngineInput,
    )

"""
    조회 도구 T1~T5·T8 (GLOW-125 2부, docs/agent-flow/tool-contracts.md 3절)
      - 엔진은 분석 입력을 스냅샷 하나(snapshot.build)로 고정한다. 도구는 실행마다 한 번 만든 그 스냅샷
        (ctx.engine.snap, engine_input.build_engine_input)을 계약 출력 모델로 **옮기기만** 한다.
        DB 를 다시 읽지 않으므로 입력 순번 뒤 변경이 섞이지 않고, 몇 번 불러도 같은 값이다.
      - T2~T5·T8 은 입력이 없다(payload None, 범위는 엔진 입력이 정함. 인터페이스 결정 3).
      - 도구는 스냅샷을 고치지 않는다. 엔진 Store 의 defaultdict 도 [] 로 읽지 않는다(없는 열쇠가 생김).
      - 형식이 계약과 다르면(예: 모르는 단계 코드) 출력 모델이 ValidationError 를 낸다 → call_tool 이 FAILED(UNEXPECTED_TOOL_ERROR).
"""

_CHANNELS = ("AMAZON", "SEPHORA")  # 엔진 snapshot.build mu_ch 와 같은 순서·값
_BASIS_DAYS = SUPPORTED_SALES_BASIS_DAYS  # 28. 계산 계약 T14-v1 고정(9절 Q6)


class EngineToolContext(Protocol):
    """조회 도구가 읽는 실행 문맥. 그래프 ToolContext(engine = EngineInput)가 그대로 맞는다."""

    @property
    def engine(self) -> EngineInput: ...


def _warehouse_ref(wh: dict[str, Any]) -> dict[str, str]:
    return {
        "warehouse_id": wh["warehouse_id"],
        "warehouse_code": wh["warehouse_code"],
        "region_code": wh["region_code"],
    }


def _us_warehouses(snap: dict[str, Any]) -> list[dict[str, Any]]:
    """도착·공여 창고 2개, warehouse_code 순"""
    return sorted((snap["dest"], snap["donor"]), key=lambda w: w["warehouse_code"])


def _company_sku_codes(snap: dict[str, Any]) -> list[str]:
    return sorted(snap["sku_attr"])


# --- T1 ---------------------------------------------------------------------------------------------


async def get_delay_context(
    ctx: EngineToolContext, payload: GetDelayContextInput
) -> DelayContext:
    """T1 지연 문맥. 입력 값은 이 실행의 엔진 입력과 같아야 한다(다르면 다른 실행을 부른 것이므로 ValueError)."""
    engine = ctx.engine
    snap = engine.snap
    expected = {
        "analysis_run_id": str(engine.analysis_run_id),
        "delay_event_id": str(engine.delay_event_id),
        "base_date": engine.base_date,
        "input_seq": engine.input_seq,
    }
    actual = {k: getattr(payload, k) for k in expected}
    if actual != expected:
        raise ValueError(
            f"T1 입력이 이 실행의 엔진 입력과 다릅니다: 입력 {actual}, 실행 {expected}"
        )
    return DelayContext.model_validate(
        {
            "shipment_id": snap["delayed_shipment_id"],
            "base_date": snap["D"],
            "planned_3pl_inbound_date": snap["P"],
            "expected_3pl_inbound_date": snap["E"],
            "stage": snap["stage"],
            "transport_mode": snap["mode"],
            "planned_kr_release_date": snap["planned_kr_release"],
            "trigger_shipment_schedule_snapshot_id": snap["trigger_snapshot_id"],
            "shipment_skus": [
                {
                    "sku_id": snap["sku_ids"][code],
                    "sku_code": code,
                    "delayed_quantity": snap["delayed_qty"].get(code, 0),
                }
                for code in sorted(snap["k_shipment"])
            ],
            "destination_warehouse": _warehouse_ref(snap["dest"]),
            "donor_warehouse": _warehouse_ref(snap["donor"]),
            "kr_origin_warehouse": _warehouse_ref(snap["kr"]),
        }
    )


# --- T2 ---------------------------------------------------------------------------------------------


async def get_inventory_snapshot(
    ctx: EngineToolContext, payload: None
) -> InventorySnapshot:
    """T2 D−1 마감 재고. 도착·공여 창고는 고객사 모든 SKU(엔진 재생 복원 값), 한국 창고는 행이 있는 SKU만."""
    snap = ctx.engine.snap
    codes = _company_sku_codes(snap)
    warehouses = []
    for wh in _us_warehouses(snap):
        rest = snap["restore"][wh["warehouse_id"]]
        warehouses.append(
            {
                "warehouse_code": wh["warehouse_code"],
                "items": [
                    {
                        "sku_code": c,
                        "on_hand_quantity": rest["on_hand"][c],
                        "reserved_quantity": rest["reserved"][c],
                    }
                    for c in codes
                ],
            }
        )
    return InventorySnapshot.model_validate(
        {
            "inventory_date": snap["D"] - timedelta(days=1),
            "warehouses": warehouses,
            "kr_on_hand": [
                {"sku_code": c, "on_hand_quantity": q}
                for c, q in sorted(snap["kr_on_hand"].items())
            ],
        }
    )


# --- T3 ---------------------------------------------------------------------------------------------


async def get_inbound_schedule(
    ctx: EngineToolContext, payload: None
) -> InboundSchedule:
    """T3 입고 예정(지연 선적·이미 입고된 선적 제외, 도착일 = max(예상일, D))과 공여 창고 다음 입고(R-03)"""
    snap = ctx.engine.snap
    inbound = []
    for wh in _us_warehouses(snap):
        for x in snap["inbound"][wh["warehouse_id"]]:
            inbound.append(
                {
                    "shipment_id": x["shipment_id"],
                    "warehouse_id": wh["warehouse_id"],
                    "warehouse_code": wh["warehouse_code"],
                    "expected_3pl_inbound_date": x["raw_expected"],
                    "arrival_date": x["arrival"],
                    "quantities": [
                        {"sku_code": c, "quantity": q}
                        for c, q in sorted(x["qty"].items())
                    ],
                }
            )
    inbound.sort(
        key=lambda r: (r["warehouse_code"], r["arrival_date"], r["shipment_id"])
    )
    return InboundSchedule.model_validate(
        {
            "inbound": inbound,
            "donor_next_inbound": [
                {"sku_code": c, "next_inbound_date": snap["next_inbound"][c]}
                for c in _company_sku_codes(snap)
            ],
        }
    )


# --- T4 ---------------------------------------------------------------------------------------------


def _basis_days(base_date: date) -> list[str]:
    """D−28 ~ D−1 (엔진 snapshot.build 의 hist)"""
    return [
        (base_date - timedelta(days=i)).isoformat() for i in range(_BASIS_DAYS, 0, -1)
    ]


async def get_sales_history(ctx: EngineToolContext, payload: None) -> SalesHistory:
    """T4 최근 28일 유효 주문 수요. 합계는 엔진 Store 의 같은 색인(demand_ch)에서 정수로 센다.

    스냅샷의 mu_ch 는 합계 ÷ 28 실수라 정수 합계를 되돌릴 수 없어서다(계약 1절 "평균 수요"). 시험이 합계 ÷ 28 = mu_ch 를 확인한다."""
    engine = ctx.engine
    snap = engine.snap
    demand_ch = engine.store.demand_ch
    days = _basis_days(snap["D"])
    totals = []
    for wh in _us_warehouses(snap):
        for code in _company_sku_codes(snap):
            sku_id = snap["sku_ids"][code]
            for ch in _CHANNELS:
                q = sum(
                    demand_ch.get((wh["warehouse_id"], sku_id, day, ch), 0)
                    for day in days
                )
                totals.append(
                    {
                        "warehouse_code": wh["warehouse_code"],
                        "sku_code": code,
                        "sales_channel": ch,
                        "quantity": q,
                    }
                )
    basis = snap["sales_basis"]
    actual = snap["actual_demand"]
    code_of_wh = {w["warehouse_id"]: w["warehouse_code"] for w in _us_warehouses(snap)}
    return SalesHistory.model_validate(
        {
            "sales_basis_type": SalesBasisType.ORDER_DEMAND,
            "basis_start_date": basis["start"],
            "basis_end_date": basis["end"],
            "basis_days": _BASIS_DAYS,
            "demand_totals": totals,
            "event_days": basis["event_days"],
            "event_names": list(basis["event_names"]),
            "calendar_version": basis["calendar_version"],
            "calendar_sha256": basis["calendar_sha256"],
            "actual_known_days": sorted(snap["actual_known_days"]),
            "actual_demand": sorted(
                (
                    {
                        "warehouse_code": code_of_wh[w],
                        "sku_code": s,
                        "demand_date": day,
                        "quantity": q,
                    }
                    for (w, s, day), q in actual.items()
                ),
                key=lambda r: (r["warehouse_code"], r["sku_code"], r["demand_date"]),
            ),
        }
    )


# --- T5 ---------------------------------------------------------------------------------------------


def _allocation_state(snap: dict[str, Any]) -> list[dict[str, Any]]:
    """D−1 배정 하루 재생 결과(엔진 ReplayReservationSource.restore). 형식은 계약 3.5절(9절 Q12를 이 작업에서 채움).

    엔진 주문 층(core.simulate_orders)이 읽는 값을 엔진 모양 그대로 둔다: 확보 뒤 출고 대기(carry)와 대기 주문 코드.
    보호·확보 수량은 SKU 코드 순 배열로 바꾼다."""
    out = []
    for wh in _us_warehouses(snap):
        rest = snap["restore"][wh["warehouse_id"]]
        out.append(
            {
                "warehouse_code": wh["warehouse_code"],
                "replay_result_sha256": rest["result_sha256"],
                "pending_dispatch": [
                    {
                        "order_code": p["order_code"],
                        "secured_date": p["secured_date"],
                        "lines": [
                            {
                                "line_no": ln["line_no"],
                                "sku_code": ln["sku_code"],
                                "quantity": ln["quantity"],
                            }
                            for ln in p["lines"]
                        ],
                    }
                    for p in sorted(rest["carry"], key=lambda p: p["order_code"])
                ],
                "waiting_order_codes": sorted(o["order_code"] for o in rest["waiting"]),
                "blocked_order_protection": [
                    {"sku_code": c, "quantity": q}
                    for c, q in sorted(rest["protection"].items())
                ],
                "secured_waiting_for_dispatch": [
                    {"sku_code": c, "quantity": q}
                    for c, q in sorted(rest["secured_wait"].items())
                ],
            }
        )
    return out


async def get_open_orders(ctx: EngineToolContext, payload: None) -> OpenOrders:
    """T5 도착·공여 창고 D−1 마감 열린 주문(엔진 복원과 같은 주문)과 배정 재생 상태"""
    snap = ctx.engine.snap
    code_of = snap["sku_code_by_id"]
    orders = []
    for wh in _us_warehouses(snap):
        for o in snap["restore"][wh["warehouse_id"]]["open_orders"]:
            lines = sorted(
                snap["lines_by_order"][o["order_id"]], key=lambda ln: int(ln["line_no"])
            )
            orders.append(
                {
                    "order_id": o["order_id"],
                    "order_code": o["order_code"],
                    "warehouse_id": o["warehouse_id"],
                    "sales_channel": o["sales_channel"],
                    "ordered_at": o["ordered_at"],
                    "promised_delivery_at": o["promised_delivery_at"],
                    "lines": [
                        {
                            "order_line_id": ln["order_line_id"],
                            "line_no": int(ln["line_no"]),
                            "sku_id": ln["sku_id"],
                            "sku_code": code_of[ln["sku_id"]],
                            "ordered_quantity": int(ln["ordered_quantity"]),
                            "line_status": ln["line_status"],
                        }
                        for ln in lines
                    ],
                }
            )
    orders.sort(key=lambda o: o["order_code"])
    return OpenOrders.model_validate(
        {"orders": orders, "allocation_state": _allocation_state(snap)}
    )


# --- T8 ---------------------------------------------------------------------------------------------


# 계약 LeadTimes 필드 ← 엔진 lt 키 (계약 3.6절)
_LEAD_FIELDS = {
    "dest_outbound_handling_days": "oh_dest",
    "dest_customer_delivery_days": "cd_same",
    "donor_outbound_handling_days": "oh_donor",
    "donor_customer_delivery_days": "cd_cross",
    "kr_outbound_handling_days": "oh_kr",
    "kr_air_transit_days": "air_kr",
    "dest_inbound_handling_days": "inbound_dest",
}


def _lead_times(engine) -> dict[str, int | None]:
    """리드타임. 계약 9절 Q5: day_basis 가 달력일이 아닌 행(BUSINESS_DAY)은 없는 행처럼 null.

    엔진은 day_basis 를 보지 않고 일수를 계산에 쓰므로 그런 행이 있으면 T8 과 계산이 어긋난다.
    그래서 입력 검증이 엔진 계산 전에 실행을 보류로 막는다(engine_operating_rules, 이슈 #61).
    실제 실행에서는 이 null 갈래까지 오지 않고, 여기서는 계약 출력 규칙만 지킨다."""
    snap = engine.snap
    chosen = lead_time_rows(engine.store, snap)
    out = {}
    for field, key in _LEAD_FIELDS.items():
        row, value = chosen[key], snap["lt"][key]
        if (row is None) != (value is None) or (
            row is not None and int(row["days"]) != value
        ):
            raise ValueError(
                f"리드타임 {key} 를 고른 행이 엔진 계산 값과 다릅니다: 행 {row}, 엔진 {value}"
            )
        out[field] = (
            value
            if row is not None and row["day_basis"] == SUPPORTED_DAY_BASIS
            else None
        )
    return out


def _bool(value: str) -> bool:
    """엔진 Store 불리언 글자('true'·'false', bool_text) → bool. 그 밖이면 ValueError"""
    if value not in ("true", "false"):
        raise ValueError(f"불리언이 아닌 값입니다: {value!r}")
    return value == "true"


async def get_operating_rules(ctx: EngineToolContext, payload: None) -> OperatingRules:
    """T8 운영 기준(D 시점 유효한 최신 판). skus[] 는 고객사 모든 SKU = 영향·관찰·선적 SKU를 모두 포함한다."""
    engine = ctx.engine
    snap = engine.snap
    rule = snap["rule"]
    return OperatingRules.model_validate(
        {
            "operating_rule_set_id": rule["operating_rule_set_id"],
            "version_no": int(rule["version_no"]),
            "safety_stock_days": snap["safety_days"],
            "sales_basis_days": snap["basis_days"],
            "lead_times": _lead_times(engine),
            "skus": [
                {
                    "sku_id": snap["sku_ids"][c],
                    "sku_code": c,
                    "emergency_air_eligible": _bool(
                        snap["sku_attr"][c]["emergency_air_eligible"]
                    ),
                }
                for c in _company_sku_codes(snap)
            ],
        }
    )
