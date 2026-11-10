from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from typing import Any

from app.modules.delay_analysis.application.agent.tools.impact_schemas import (
    AtRiskOrder,
)
from app.modules.delay_analysis.application.agent.tools.lookup_schemas import (
    OpenOrder,
)
from app.modules.delay_analysis.application.agent.tools.option_schemas import (
    CalculateOptionOutcomesInput,
    OptionOutcomes,
    OptionPrecheck,
    Precheck,
)
from app.modules.delay_analysis.infrastructure.analysis_enums import (
    OptionReasonCode,
    OptionType,
)
from app.modules.delay_analysis.infrastructure.engine.engine_loader import (
    load_allocation_module,
)

"""
    T10 calculate_option_outcomes (docs/agent-flow/tool-contracts.md 6절, docs/analysis/option-outcomes.md)
      - 엔진 options.evaluate·pipeline.aggregates·storage.py(대응안·주문 결과 행)를 계약 입력(T1~T9 출력)만으로 다시 계산한다.
        엔진처럼 대응안마다 도착 창고 예측 층(core.forecast_sku)과 주문 배정(core.simulate_orders)을 다시 돈다.
      - 주문 배정은 데이터 팀 공유 배정 모듈(allocation_core.simulate, 규칙 D00-B-b2-v1)을 해시 대조한 엔진 폴더에서 불러 쓴다.
        배정 규칙을 서버에 다시 쓰지 않아 엔진과 어긋나지 않는다(사용자 결정 11-09). 시험은 simulate 를 바꿔 끼운다.
      - 최종 feasibility_status 는 여기서 정한다(9절 Q2). 판정 순서는 6절 표, 사유는 6.1절 표 A·B·C.
      - 숫자 규칙은 엔진 common 과 같다: raw 값은 Decimal(repr(float)) → 소수 4자리 half-even, 정수화는 그 값에서 올림·내림.
      - 입력끼리 맞지 않으면(T6 원값과 다시 계산한 예측, T9 공여 판정과 다시 계산한 판정, 결과 행 수) ValueError(FAILED).
"""

O, R = OptionType, OptionReasonCode
_Q4 = Decimal("0.0001")
_ALLOCATION_RULE = (
    "D00-B-b2-v1"  # 엔진 common.ALLOCATION_RULE. 배정 모듈이 정책 버전을 대조한다
)
_SCOPE = "T10"  # 배정 입력의 고객사 코드 자리. 모든 주문에 같은 값을 넣어 범위 판정만 통과시킨다
_CALCULATION_CONTRACT = "T14-v1"
_BASIS_DAYS = 28  # 계산 계약 T14-v1 고정(9절 Q6)

# allocation_core.simulate(첫날 입력, 다음 날들) → 날짜별 출력
Simulate = Callable[[dict[str, Any], list[dict[str, Any]]], list[dict[str, Any]]]


def _engine_simulate(
    first: dict[str, Any], schedule: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    return load_allocation_module().simulate(first, schedule)


# --- 엔진 common 과 같은 숫자 규칙 ------------------------------------------------------------------


def _q4(x: Any) -> Decimal:
    d = x if isinstance(x, Decimal) else Decimal(repr(float(x)))
    return d.quantize(_Q4, rounding=ROUND_HALF_EVEN)


def _ceil(x: Any) -> int:
    return int(_q4(x).to_integral_value(rounding=ROUND_CEILING))


def _floor(x: Any) -> int:
    return int(_q4(x).to_integral_value(rounding=ROUND_FLOOR))


def _s4(x: Any) -> str:
    return f"{_q4(x):.4f}"


def _days(start: date, end: date) -> list[date]:
    """[start, end) 날짜 목록"""
    return [start + timedelta(i) for i in range((end - start).days)]


# --- 입력 정리 ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _RiskLine:
    order_line_id: str
    line_no: int
    sku: str
    open_quantity: int


@dataclass(frozen=True)
class _Risk:
    """T7 위험 주문 한 건(엔진 ol["risks"] 한 항목)"""

    order_code: str
    ordered: date  # 접수 시각의 UTC 날짜(엔진 common.d)
    promised: date
    promised_at: str
    delay_induced: bool
    lines: tuple[_RiskLine, ...]


@dataclass
class _Sim:
    """주문 배정 시뮬레이션에서 T10 이 읽는 것: 주문별 확보일, 날마다 줄별 보호 수량, 구간 끝"""

    secured: dict[str, date]
    outs: dict[date, dict[tuple[str, int], int]]
    h_end: date


@dataclass
class _Forecast:
    raw: float
    daily: dict[date, float]


@dataclass
class _Outcome:
    """위험 주문 한 건의 대응안 결과(엔진 outcomes 한 항목)"""

    risk: _Risk
    recovered: bool
    delivery: date | None
    secured: dict[str, int]  # order_line_id → 확보 수량
    source: str
    source_warehouse_id: str
    reason: str | None = None


@dataclass
class _Option:
    """대응안 한 개의 계산 중간값(엔진 evaluate 의 o)"""

    option_type: OptionType
    supply_date: date | None = None
    items: list[dict[str, Any]] = field(default_factory=list)
    outcomes: list[_Outcome] | None = None
    reason_code: str | None = None
    specific_reason: str | None = None
    status: str | None = None
    supply_total: int | None = 0
    all_unknown: bool = False
    unknown_impacted: bool = False
    delay_induced_recovered: int | None = None
    committed_total: int | None = None


class _World:
    """계약 입력을 엔진 스냅샷이 쓰는 모양으로 정리한 것. 값은 옮기기만 하고 고치지 않는다."""

    def __init__(self, data: CalculateOptionOutcomesInput, simulate: Simulate):
        ctx, rules, impact = (
            data.delay_context,
            data.operating_rules,
            data.inventory_impact,
        )
        self.simulate = simulate
        self.D = ctx.base_date
        self.E = ctx.expected_3pl_inbound_date
        self.P = ctx.planned_3pl_inbound_date
        self.dest, self.donor, self.kr = (
            ctx.destination_warehouse,
            ctx.donor_warehouse,
            ctx.kr_origin_warehouse,
        )
        lt = rules.lead_times
        self.oh_dest = lt.dest_outbound_handling_days
        self.cd_same = lt.dest_customer_delivery_days
        self.oh_donor = lt.donor_outbound_handling_days
        self.cd_cross = lt.donor_customer_delivery_days
        if self.oh_dest is None or self.cd_same is None:
            # 엔진은 이 경우 snapshot.build 에서 보류한다(MISSING_REQUIRED_DATA). 그래프는 보류 실행에서 T10 을 부르지 않는다
            raise ValueError(
                "도착 창고 출고 처리·고객 배송 리드타임이 없습니다(보류 실행에서 부름)"
            )
        self.safety_days = rules.safety_stock_days
        self.delayed_qty: dict[str, int] = defaultdict(int)
        for s in ctx.shipment_skus:
            self.delayed_qty[s.sku_code] += s.delayed_quantity
        self.air_eligible = {s.sku_code: s.emergency_air_eligible for s in rules.skus}
        self.kr_on_hand = {
            k.sku_code: k.on_hand_quantity for k in data.inventory_snapshot.kr_on_hand
        }
        stock = {w.warehouse_code: w.items for w in data.inventory_snapshot.warehouses}
        self.on_hand = {
            code: {i.sku_code: i.on_hand_quantity for i in items}
            for code, items in stock.items()
        }
        self.reserved = {
            code: {i.sku_code: i.reserved_quantity for i in items}
            for code, items in stock.items()
        }
        self.orders: dict[str, list[OpenOrder]] = defaultdict(list)
        for o in data.open_orders.orders:
            self.orders[o.warehouse_id].append(o)
        self.allocation = {
            a["warehouse_code"]: a for a in data.open_orders.allocation_state
        }
        self.inbound: dict[str, list[tuple[date, dict[str, int]]]] = defaultdict(list)
        for x in data.inbound_schedule.inbound:
            self.inbound[x.warehouse_id].append(
                (x.arrival_date, {q.sku_code: q.quantity for q in x.quantities})
            )
        self.next_inbound = {
            n.sku_code: n.next_inbound_date
            for n in data.inbound_schedule.donor_next_inbound
        }
        sales = data.sales_history
        self.demand = {
            (t.warehouse_code, t.sku_code, t.sales_channel.value): t.quantity
            for t in sales.demand_totals
        }
        self.known_days = set(sales.actual_known_days)
        self.actual = {
            (a.warehouse_code, a.sku_code, a.demand_date): a.quantity
            for a in sales.actual_demand
        }
        self.items = {i.sku_code: i for i in impact.items}
        self.fc_skus = sorted(self.items)
        self.k_imp = list(impact.impacted_sku_codes)
        self.risks = [_risk(r) for r in data.at_risk_orders.risks]
        self.h_end = data.at_risk_orders.simulation_horizon_end
        plan = data.at_risk_orders.expected_plan_simulation
        self.keep_sim = _Sim(
            secured={s.order_code: s.secured_date for s in plan.secured_dates},
            outs={
                day.date: {
                    (ln.order_code, ln.line_no): ln.protected for ln in day.lines
                }
                for day in plan.days
            },
            h_end=self.h_end,
        )
        # P·E 예측(엔진 fc 의 base·total). 대응안의 감소량은 이 원값(부동소수)으로 계산한다
        self.base = {s: self.forecast(s, self.P) for s in self.fc_skus}
        self.total = {s: self.forecast(s, self.E) for s in self.fc_skus}
        for s in self.fc_skus:
            item = self.items[s]
            if (_s4(self.base[s].raw), _s4(self.total[s].raw)) != (
                item.raw_baseline_shortage,
                item.raw_total_shortage,
            ):
                raise ValueError(
                    f"{s} 예측 원값을 다시 계산한 값이 T6 출력과 다릅니다(다른 실행의 입력인지 확인)"
                )

    # 엔진 snapshot.build mu: 28일 합계 ÷ 28 (채널 합은 두 채널 합계를 더한 정수에서)
    def mu(self, wh_code: str, sku: str) -> float:
        return (
            self.demand.get((wh_code, sku, "AMAZON"), 0)
            + self.demand.get((wh_code, sku, "SEPHORA"), 0)
        ) / _BASIS_DAYS

    def mu_ch(self, wh_code: str, sku: str, channel: str) -> float:
        return self.demand.get((wh_code, sku, channel), 0) / _BASIS_DAYS

    def commit(self, warehouse_id: str) -> dict[str, int]:
        """열린 주문 활성 상세의 주문 수량 합(엔진 forecast_sku·_other_region 의 commit)"""
        out: dict[str, int] = defaultdict(int)
        for o in self.orders[warehouse_id]:
            for ln in o.lines:
                if ln.line_status == "ACTIVE":
                    out[ln.sku_code] += ln.ordered_quantity
        return out

    def delivery_date(self, secured_day: date) -> date:
        return secured_day + timedelta(self.oh_dest + self.cd_same)

    def forecast(
        self,
        sku: str,
        delayed_arrival: date,
        extra_supply: Iterable[tuple[date, int]] = (),
    ) -> _Forecast:
        """엔진 core.forecast_sku 중 대응안이 읽는 값(부족 원값·일별 부족)"""
        code, wid = self.dest.warehouse_code, self.dest.warehouse_id
        on0 = self.on_hand[code].get(sku, 0)
        commit = self.commit(wid)[sku]
        m = self.mu(code, sku)
        sup = [(a, q.get(sku, 0)) for a, q in self.inbound[wid] if q.get(sku, 0)]
        sup += [(a, q) for a, q in extra_supply if q]
        dq = self.delayed_qty.get(sku, 0)
        short, cum_f, daily = 0.0, 0.0, {}
        for t in _days(self.D, self.E):
            a_day = self.actual.get((code, sku, t), 0) if t in self.known_days else None
            cum_f += a_day if a_day is not None else m
            a = (
                on0
                - commit
                + sum(q for x, q in sup if x <= t)
                + (dq if delayed_arrival <= t else 0)
            )
            deficit = max(0.0, cum_f - a) if a >= 0 else cum_f
            daily[t] = deficit
            short = max(short, deficit)
        return _Forecast(raw=short, daily=daily)

    def forecast_reduction(self, sku: str, **kw: Any) -> Decimal:
        """엔진 options._forecast_reduction: 대응안 뒤 남는 증분과의 차이(원값으로 계산 후 소수 4자리)"""
        after = self.forecast(
            sku, kw.get("delayed_arrival", self.E), kw.get("extra_supply", ())
        )
        base, total = self.base[sku].raw, self.total[sku].raw
        inc = max(0.0, total - base)
        return _q4(max(0.0, inc - max(0.0, after.raw - base)))

    def simulate_orders(
        self,
        delayed_arrival: date,
        extra_receipts: Iterable[tuple[date, str, int]] = (),
    ) -> _Sim:
        """엔진 core.simulate_orders: 도착 창고의 D 하루부터 구간 끝까지 배정(allocation_core.simulate)"""
        code, wid = self.dest.warehouse_code, self.dest.warehouse_id
        state = self.allocation[code]
        orders = {o.order_code: o for o in self.orders[wid]}
        waiting = [_alloc_order(orders[c], code) for c in state["waiting_order_codes"]]
        pending = state["pending_dispatch"]
        receipts: dict[date, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for arrival, qty in self.inbound[wid]:
            for s, q in qty.items():
                receipts[arrival][s] += q
        for s, q in self.delayed_qty.items():
            receipts[delayed_arrival][s] += q
        for a, s, q in extra_receipts:
            receipts[a][s] += q
        first = {
            "policy": "v5",
            "policy_version": _ALLOCATION_RULE,
            "date": self.D.isoformat(),
            "company_code": _SCOPE,
            "warehouse_code": code,
            "on_hand_start": dict(self.on_hand[code]),
            "receipts": dict(receipts.get(self.D, {})),
            "pending_dispatch": pending,
            "orders": waiting,
        }
        schedule = [
            {
                "date": t.isoformat(),
                "receipts": dict(receipts.get(t, {})),
                "new_orders": [],
            }
            for t in _days(self.D + timedelta(1), self.h_end + timedelta(1))
        ]
        outs = self.simulate(first, schedule)
        secured = {p["order_code"]: _day(p["secured_date"]) for p in pending}
        for out in outs:
            for c, v in out["orders"].items():
                if v["status"] == "SECURED_WAITING_DISPATCH" and c not in secured:
                    secured[c] = _day(v["secured_date"])
        return _Sim(
            secured=secured,
            outs={
                _day(out["date"]): {
                    (ln["order_code"], int(ln["line_no"])): int(ln["protected"])
                    for ln in out["lines"]
                }
                for out in outs
            },
            h_end=self.h_end,
        )


def _day(value: Any) -> date:
    """엔진 common.d: date 이거나 앞 10자리가 YYYY-MM-DD 인 문자열"""
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _alloc_order(o: OpenOrder, warehouse_code: str) -> dict[str, Any]:
    """엔진 snapshot.to_alloc_order 와 같은 배정 입력 주문(출고 수량 0, 줄 번호 순)"""
    return {
        "company_code": _SCOPE,
        "warehouse_code": warehouse_code,
        "order_code": o.order_code,
        "ordered_at": o.ordered_at,
        "promised_delivery_at": o.promised_delivery_at,
        "creation_status": "ACTIVE",
        "lines": [
            {
                "line_no": ln.line_no,
                "sku_code": ln.sku_code,
                "ordered_quantity": ln.ordered_quantity,
                "shipped_quantity": 0,
                "line_status": ln.line_status.value,
            }
            for ln in sorted(o.lines, key=lambda x: x.line_no)
        ],
    }


def _risk(r: AtRiskOrder) -> _Risk:
    return _Risk(
        order_code=r.order_code,
        ordered=_day(r.ordered_at),
        promised=_day(r.promised_delivery_at),
        promised_at=r.promised_delivery_at,
        delay_induced=r.risk_attribution == "DELAY_INDUCED",
        lines=tuple(
            _RiskLine(ln.order_line_id, ln.line_no, ln.sku_code, ln.open_quantity)
            for ln in r.lines
        ),
    )


# --- 주문 결과 ---------------------------------------------------------------------------------------


def _securement(w: _World, sim: _Sim, r: _Risk) -> dict[str, int]:
    """엔진 options._securement: 판정 기준일의 상세별 확보 수량(출고 배정 또는 보호)"""
    judge = max(w.D, r.promised - timedelta(w.oh_dest + w.cd_same))
    j = min(judge, sim.h_end)
    sd = sim.secured.get(r.order_code)
    if sd is not None and sd <= j:
        return {ln.order_line_id: ln.open_quantity for ln in r.lines}
    day = sim.outs[j] if j in sim.outs else sim.outs[max(sim.outs)]
    return {
        ln.order_line_id: min(day.get((r.order_code, ln.line_no), 0), ln.open_quantity)
        for ln in r.lines
    }


def _outcomes_from_sim(
    w: _World,
    sim: _Sim,
    source: str,
    source_warehouse_id: str,
    base_secured: Callable[[_Risk], bool],
) -> list[_Outcome]:
    out = []
    for r in w.risks:
        sd = sim.secured.get(r.order_code)
        dl = w.delivery_date(sd) if sd else None
        rec = dl is not None and dl <= r.promised
        out.append(
            _Outcome(
                risk=r,
                recovered=rec,
                delivery=dl,
                secured=_securement(w, sim, r),
                source=source if rec and not base_secured(r) else "EXISTING_STOCK",
                source_warehouse_id=source_warehouse_id,
            )
        )
    return out


def _no_supply_outcomes(
    w: _World, option_type: OptionType, supply_date: date, option_reason: str
) -> list[_Outcome]:
    """계산 완료 NOT_FEASIBLE(G-3): 위험 주문 전부 NOT_RECOVERED, 확보 0. 사유는 6.1절 표 B"""
    out = []
    for r in w.risks:
        if supply_date + timedelta(w.oh_dest + w.cd_same) > r.promised:
            why = "PROMISE_UNREACHABLE_LEADTIME"
        elif option_type == O.KR_AIR_REPLENISH and (
            option_reason == "NO_KR_ELIGIBLE_STOCK"
            or any(not w.air_eligible[ln.sku] for ln in r.lines)
        ):
            why = "NO_KR_ELIGIBLE_STOCK"
        else:
            why = "NO_RECOVERY_EFFECT"
        out.append(
            _Outcome(
                risk=r,
                recovered=False,
                delivery=None,
                secured={ln.order_line_id: 0 for ln in r.lines},
                source="EXISTING_STOCK",
                source_warehouse_id=w.dest.warehouse_id,
                reason=why,
            )
        )
    return out


def _order_reason(w: _World, o: _Option, x: _Outcome) -> str:
    """엔진 options._order_reason: 사유가 아직 없는 미회복 주문(6.1절 표 B). KEEP_PLAN 은 효과 없음"""
    r = x.risk
    if (
        o.option_type in (O.KR_AIR_REPLENISH, O.PRE_DEPARTURE_AIR)
        and o.supply_date is not None
    ):
        if o.supply_date + timedelta(w.oh_dest + w.cd_same) > r.promised:
            return "PROMISE_UNREACHABLE_LEADTIME"
        if o.option_type == O.KR_AIR_REPLENISH and any(
            not w.air_eligible[ln.sku] for ln in r.lines
        ):
            return "NO_KR_ELIGIBLE_STOCK"
    return "NO_RECOVERY_EFFECT"


# --- SKU 항목(sku_results.v1) ------------------------------------------------------------------------


def _item(
    w: _World,
    sku: str,
    red: Decimal,
    *,
    status: str = "EVALUATED",
    reason: str | None = None,
    supply: int | None = None,
    supply_date: date | None = None,
    donor: dict[str, Any] | None = None,
    null_effect: bool = False,
) -> dict[str, Any]:
    """엔진 options._item. null_effect: 전체 판정 불가(G-2)의 판정 불가 SKU — 효과·공급 칸 null, 예측 층 값은 유지"""
    v = w.items[sku]
    increment = Decimal(v.raw_increment_shortage)
    residual = increment - red
    impacted = sku in w.k_imp
    return {
        "sku_id": v.sku_id,
        "sku_scope": v.sku_scope,
        "impact_source": None
        if not impacted
        else "FORECAST_INCREMENT"
        if increment > 0
        else "DELAY_INDUCED_ORDER_LINE",
        "evaluation_status": status,
        "not_estimable_reason": reason,
        "raw_baseline_shortage": v.raw_baseline_shortage,
        "raw_increment_shortage": v.raw_increment_shortage,
        "raw_reduction": None if null_effect else _s4(red),
        "raw_residual_increment": None if null_effect else _s4(residual),
        "baseline_prepare_quantity": v.baseline_prepare_quantity,
        "increment_prepare_quantity": v.increment_prepare_quantity,
        "reduction_committed_quantity": None if null_effect else _floor(red),
        "residual_prepare_quantity": None if null_effect else _ceil(residual),
        "supply_quantity": None if null_effect else supply,
        "supply_available_date": supply_date
        if supply_date and not null_effect
        else None,
        "donor": donor,
    }


# --- 대응안별 계산 -----------------------------------------------------------------------------------


def _keep_plan(w: _World, o: _Option, reds: dict[str, Decimal]) -> None:
    o.supply_date = w.E
    o.outcomes = _outcomes_from_sim(
        w, w.keep_sim, "EXISTING_STOCK", w.dest.warehouse_id, lambda r: False
    )
    o.items = [_item(w, s, reds[s]) for s in w.fc_skus]


def _pre_departure_air(
    w: _World, o: _Option, pre: OptionPrecheck, reds: dict[str, Decimal]
) -> None:
    S = pre.supply_available_date
    o.supply_date = S
    if (
        pre.precheck == Precheck.NO_SUPPLY
    ):  # S ≥ E: 계산 완료 NOT_FEASIBLE(G-3), 감소 0·공급 0
        o.specific_reason = pre.reason_code.value
        o.outcomes = _no_supply_outcomes(w, o.option_type, S, o.specific_reason)
        o.items = [_item(w, s, reds[s], supply=0, supply_date=S) for s in w.fc_skus]
        return
    sim = w.simulate_orders(S)
    keep = w.keep_sim.secured

    def keep_recovers(r: _Risk) -> bool:
        sd = keep.get(r.order_code)
        return bool(sd) and w.delivery_date(sd) <= r.promised

    o.outcomes = _outcomes_from_sim(
        w, sim, "PRE_DEPARTURE_AIR", w.dest.warehouse_id, keep_recovers
    )
    for s in w.fc_skus:
        reds[s] = w.forecast_reduction(s, delayed_arrival=S)
    o.items = [
        _item(w, s, reds[s], supply=w.delayed_qty.get(s, 0), supply_date=S)
        for s in w.fc_skus
    ]
    o.supply_total = sum(w.delayed_qty.get(s, 0) for s in w.fc_skus)


def _kr_air_replenish(
    w: _World, o: _Option, pre: OptionPrecheck, reds: dict[str, Decimal]
) -> None:
    ks = pre.supply_available_date
    o.supply_date = ks
    hit = list(pre.eligible_sku_codes)

    def item(s: str, supply: int) -> dict[str, Any]:
        applies = s in hit
        return _item(
            w,
            s,
            reds[s],
            status="EVALUATED" if applies else "NOT_APPLICABLE",
            supply=supply if applies else None,
            supply_date=ks if applies else None,
        )

    if pre.precheck == Precheck.NO_SUPPLY:  # 계산 완료 NOT_FEASIBLE(G-3), 감소 0·공급 0
        o.specific_reason = pre.reason_code.value
        o.outcomes = _no_supply_outcomes(w, o.option_type, ks, o.specific_reason)
        o.items = [item(s, 0) for s in w.fc_skus]
        return
    qty = {}
    for s in hit:
        daily = w.total[s].daily
        need = max([v for t, v in daily.items() if t >= ks] or [0.0])
        qty[s] = min(w.kr_on_hand.get(s, 0), _ceil(need))
    sim = w.simulate_orders(w.E, [(ks, s, q) for s, q in qty.items() if q])
    o.outcomes = _outcomes_from_sim(
        w, sim, "KR_AIR", w.kr.warehouse_id, lambda r: False
    )
    for s in w.fc_skus:
        if qty.get(s):
            reds[s] = w.forecast_reduction(s, extra_supply=[(ks, qty[s])])
    o.items = [item(s, qty.get(s, 0)) for s in w.fc_skus]
    o.supply_total = sum(qty.values())


def _donor_forecast_until(w: _World, sku: str, until: date) -> float:
    """엔진 options.donor_forecast_until: 공여 창고 [D, N) 예측 수요. 관측일이면 실제 수요, 아니면 채널 합 μ"""
    code = w.donor.warehouse_code
    mu_all = sum(w.mu_ch(code, sku, ch) for ch in ("AMAZON", "SEPHORA"))
    return sum(
        w.actual.get((code, sku, t), 0) if t in w.known_days else mu_all
        for t in _days(w.D, until)
    )


def _other_region(w: _World, o: _Option, reds: dict[str, Decimal]) -> None:
    """엔진 options._other_region: 공여 창고에서 넘길 수 있는 재고(풀)로 위험 주문을 약속 순서대로 채운다"""
    o.supply_date = w.D
    donor_id, donor_code = w.donor.warehouse_id, w.donor.warehouse_code
    on_hand, reserved = w.on_hand[donor_code], w.reserved[donor_code]
    commit = w.commit(donor_id)
    rec_d: dict[str, int] = defaultdict(int)
    for arrival, qty in w.inbound[donor_id]:
        if arrival == w.D:
            for s, q in qty.items():
                rec_d[s] += q
    need_skus = set(w.k_imp) | {ln.sku for r in w.risks for ln in r.lines}
    pool: dict[str, int] = {}
    donor_info: dict[str, dict[str, Any] | None] = {}
    for s in sorted(need_skus):
        n = w.next_inbound.get(s)
        if n is None:
            donor_info[s] = None
            continue
        fcst = _donor_forecast_until(
            w, s, n
        )  # 공여 풀·보호량은 D 시점 정적(Q-D4), 수요만 관측 반영(U6)
        ss = _ceil(w.mu(donor_code, s) * w.safety_days)
        raw = on_hand.get(s, 0) + rec_d[s] - commit[s] - fcst - ss
        pool[s] = _floor(max(0.0, raw))
        donor_info[s] = {
            "N": n,
            "on_hand": on_hand.get(s, 0),
            "reserved": reserved.get(s, 0),
            "commit_net": commit[s] - reserved.get(s, 0),
            "fcst": fcst,
            "ss": ss,
            "raw": max(0.0, raw),
            "qty": pool[s],
        }
    if w.k_imp and all(donor_info.get(s) is None for s in w.k_imp):
        _other_all_unknown(w, o, commit, donor_info)
        return
    used: dict[str, int] = defaultdict(int)
    outcomes, reasons = [], []
    for r in sorted(
        w.risks, key=lambda r: (r.promised_at, r.ordered.isoformat(), r.order_code)
    ):
        need: dict[str, int] = defaultdict(int)
        for ln in r.lines:
            need[ln.sku] += ln.open_quantity
        cross = max(r.ordered, w.D) + timedelta(w.oh_donor + w.cd_cross)
        reason = None
        if cross > r.promised:
            reason = "PROMISE_UNREACHABLE_LEADTIME"
        elif any(donor_info.get(s) is None for s in need):
            reason = "DONOR_NO_NEXT_INBOUND"
        elif any(pool.get(s, 0) - used[s] < q for s, q in need.items()):
            reason = "NO_TRANSFERABLE_STOCK"
        if reason is None:
            for s, q in need.items():
                used[s] += q
            outcomes.append(
                _Outcome(
                    risk=r,
                    recovered=True,
                    delivery=cross,
                    secured={ln.order_line_id: ln.open_quantity for ln in r.lines},
                    source="OTHER_REGION_WAREHOUSE",
                    source_warehouse_id=donor_id,
                )
            )
        else:
            reasons.append(reason)
            outcomes.append(
                _Outcome(
                    risk=r,
                    recovered=False,
                    delivery=None,
                    secured=_securement(w, w.keep_sim, r),
                    source="EXISTING_STOCK",
                    source_warehouse_id=w.dest.warehouse_id,
                    reason=reason,
                )
            )
    outcomes.sort(key=lambda x: x.risk.order_code)
    dest_code = w.dest.warehouse_code
    items, supply_total = [], 0
    for s in w.fc_skus:
        info = donor_info.get(s, "NA")
        fsec = 0.0
        mu = w.mu(dest_code, s)
        share = w.mu_ch(dest_code, s, "SEPHORA") / mu if mu > 0 else 0.0
        if s in w.k_imp and info not in (None, "NA"):
            left = pool.get(s, 0) - used[s]
            tot, base = w.total[s].raw, w.base[s].raw
            fsec = min(float(left), tot * share)
            reds[s] = _q4(
                max(0.0, max(0.0, tot - base) - max(0.0, (tot - fsec) - base))
            )
        if s in w.k_imp and info is None:
            o.unknown_impacted = True
        donor = None
        if isinstance(info, dict):
            donor = {
                "source_warehouse_id": donor_id,
                "next_inbound_basis": "N2",
                "next_inbound_date": info["N"],
                "on_hand": info["on_hand"],
                "reserved": info["reserved"],
                "open_commitment_net": info["commit_net"],
                "safety_stock_quantity": info["ss"],
                "forecast_until_next_raw": _s4(info["fcst"]),
                "transferable_raw": _s4(info["raw"]),
                "transferable_quantity": info["qty"],
                "order_layer_used_quantity": used[s],
                "forecast_channel_share": _s4(share if mu > 0 else 0),
                "forecast_securable_quantity": _floor(fsec),
            }
        sup = used[s] + _floor(fsec) if isinstance(info, dict) else None
        supply_total += sup or 0
        items.append(
            _item(
                w,
                s,
                reds[s],
                status="NOT_ESTIMABLE" if info is None else "EVALUATED",
                reason="DONOR_NO_NEXT_INBOUND" if info is None else None,
                supply=sup,
                supply_date=w.D if sup is not None else None,
                donor=donor,
            )
        )
    # 대표 사유(6.1절 표 C): 위험 주문이 모두 회복되지 않았으면 우선순위가 가장 높은 사유, 아니면 풀이 모두 0일 때만
    specific = None
    if reasons and len(reasons) == len(w.risks):
        specific = next(code for code in _PRIORITY if code in reasons)
    if specific is None and pool and all(v == 0 for v in pool.values()):
        specific = "NO_TRANSFERABLE_STOCK"
    o.outcomes, o.items, o.specific_reason, o.supply_total = (
        outcomes,
        items,
        specific,
        supply_total,
    )


# 엔진 options.PRIORITY (D-02 4.1, 계약 6.1절 표 C)
_PRIORITY = [
    "MISSING_OPERATING_RULE",
    "MISSING_REQUIRED_DATA",
    "DONOR_NO_NEXT_INBOUND",
    "ARRIVES_AFTER_DELAYED_SHIPMENT",
    "PROMISE_UNREACHABLE_LEADTIME",
    "NO_KR_ELIGIBLE_STOCK",
    "NO_TRANSFERABLE_STOCK",
    "FORECAST_EFFECT_BELOW_ONE_UNIT",
    "NO_RECOVERY_EFFECT",
]


def _other_all_unknown(
    w: _World,
    o: _Option,
    commit: dict[str, int],
    donor_info: dict[str, dict[str, Any] | None],
) -> None:
    """엔진 options._other_all_unknown(G-2): K_IMPACTED 전부 공여 다음 입고 없음 → 주문 계산·예측 확보를 하지 않는다.
    판정 불가 SKU 는 효과 null, donor 는 D 시점 입력 칸만 채우고 N 에 기대는 칸은 null."""
    donor_id, donor_code, dest_code = (
        w.donor.warehouse_id,
        w.donor.warehouse_code,
        w.dest.warehouse_code,
    )
    on_hand, reserved = w.on_hand[donor_code], w.reserved[donor_code]
    zero = _q4(0)
    items = []
    for s in w.fc_skus:
        info = donor_info.get(s, "NA")
        if info == "NA":  # 공여 계산 대상 밖 SKU: 감소 0, 공급·donor 없음
            items.append(_item(w, s, zero))
            continue
        if info is None:
            donor = {
                "source_warehouse_id": donor_id,
                "next_inbound_basis": "N2",
                "next_inbound_date": None,
                "on_hand": on_hand.get(s, 0),
                "reserved": reserved.get(s, 0),
                "open_commitment_net": commit[s] - reserved.get(s, 0),
                "safety_stock_quantity": _ceil(w.mu(donor_code, s) * w.safety_days),
                "forecast_until_next_raw": None,
                "transferable_raw": None,
                "transferable_quantity": None,
                "order_layer_used_quantity": None,
                "forecast_channel_share": None,
                "forecast_securable_quantity": None,
            }
            items.append(
                _item(w, s, zero, status="NOT_ESTIMABLE", reason="DONOR_NO_NEXT_INBOUND",
                      donor=donor, null_effect=True)
            )  # fmt: skip
            continue
        mu = w.mu(dest_code, s)
        donor = {
            "source_warehouse_id": donor_id,
            "next_inbound_basis": "N2",
            "next_inbound_date": info["N"],
            "on_hand": info["on_hand"],
            "reserved": info["reserved"],
            "open_commitment_net": info["commit_net"],
            "safety_stock_quantity": info["ss"],
            "forecast_until_next_raw": _s4(info["fcst"]),
            "transferable_raw": _s4(info["raw"]),
            "transferable_quantity": info["qty"],
            "order_layer_used_quantity": 0,
            "forecast_channel_share": _s4(
                w.mu_ch(dest_code, s, "SEPHORA") / mu if mu > 0 else 0
            ),
            "forecast_securable_quantity": 0,
        }
        items.append(_item(w, s, zero, supply=0, supply_date=w.D, donor=donor))
    o.outcomes, o.items, o.all_unknown, o.unknown_impacted, o.supply_total = (
        None,
        items,
        True,
        True,
        None,
    )


# --- 판정·출력 -----------------------------------------------------------------------------------------


def _judge(w: _World, o: _Option, reds: dict[str, Decimal]) -> None:
    """판정 순서(계약 6절 표, 엔진 evaluate 169~194행)와 미회복 주문의 사유"""
    if o.reason_code == "MISSING_OPERATING_RULE":  # 1
        o.status = "NEEDS_CHECK"
    elif o.all_unknown:  # 2
        o.status, o.reason_code = "NEEDS_CHECK", "DONOR_NO_NEXT_INBOUND"
    elif (
        o.outcomes is None
    ):  # 엔진에만 있는 단계. 1·2 가 아니면 늘 주문 결과가 있어 오지 않는다
        o.status = "NOT_FEASIBLE"
    else:
        rec_di = sum(1 for x in o.outcomes if x.recovered and x.risk.delay_induced)
        committed = sum(_floor(reds[s]) for s in w.k_imp)
        raw_red = sum(reds[s] for s in w.k_imp)
        o.delay_induced_recovered, o.committed_total = rec_di, committed
        if o.option_type == O.KEEP_PLAN:  # 3
            o.status, o.reason_code = "FEASIBLE", None
            o.delay_induced_recovered, o.committed_total = 0, 0
        elif rec_di >= 1 or committed >= 1:  # 4
            o.status, o.reason_code = "FEASIBLE", None
        elif o.unknown_impacted:  # 5
            o.status, o.reason_code = "NEEDS_CHECK", "DONOR_NO_NEXT_INBOUND"
        elif raw_red > 0:  # 6
            o.status, o.reason_code = "NOT_FEASIBLE", "FORECAST_EFFECT_BELOW_ONE_UNIT"
        elif o.specific_reason == "DONOR_NO_NEXT_INBOUND":  # 7 (9절 Q9)
            o.status, o.reason_code = "NEEDS_CHECK", "DONOR_NO_NEXT_INBOUND"
        else:  # 8
            o.status = "NOT_FEASIBLE"
            o.reason_code = o.specific_reason or "NO_RECOVERY_EFFECT"
    for x in o.outcomes or []:
        if not x.recovered:
            x.reason = x.reason or _order_reason(w, o, x)


def _option_row(w: _World, o: _Option, required: int) -> dict[str, Any]:
    """대응안 1개 → 계약 6절 출력(엔진 pipeline.aggregates, storage.py 112~134행)"""
    rows = []
    if o.outcomes is None:
        fully = delay_rec = not_rec = blocked = None
    else:
        recovered = [x for x in o.outcomes if x.recovered]
        missed = [x for x in o.outcomes if not x.recovered]
        fully, not_rec = len(recovered), len(missed)
        delay_rec = o.delay_induced_recovered
        blocked = sum(1 for x in missed if any(v > 0 for v in x.secured.values()))
        for x in o.outcomes:
            for ln in x.risk.lines:
                sec = x.secured.get(ln.order_line_id, 0)
                rows.append(
                    {
                        "order_line_id": ln.order_line_id,
                        "outcome": "RECOVERED" if x.recovered else "NOT_RECOVERED",
                        "not_recovered_reason": None if x.recovered else x.reason,
                        "line_securement_status": "FULLY_SECURED"
                        if sec >= ln.open_quantity
                        else "PARTIALLY_SECURED"
                        if sec > 0
                        else "NOT_SECURED",
                        "secured_quantity": sec,
                        "recovered_quantity": ln.open_quantity if x.recovered else 0,
                        "expected_delivery_at": f"{x.delivery.isoformat()}T23:59:59Z"
                        if x.recovered and x.delivery
                        else None,
                        "source_warehouse_id": x.source_warehouse_id
                        if sec > 0
                        else None,
                        "supply_source": x.source if sec > 0 else None,
                    }
                )
    return {
        "option_type": o.option_type,
        "feasibility_status": o.status,
        "reason_code": o.reason_code,
        "required_quantity": required,
        "securable_quantity": o.supply_total if o.outcomes is not None else None,
        "reduction_committed_total": o.committed_total,
        "expected_delivery_date": o.supply_date,
        "fully_recovered_orders": fully,
        "delay_induced_recovered_orders": delay_rec,
        "item_secured_blocked_orders": blocked,
        "not_recovered_orders": not_rec,
        "sku_results": {
            "schema_version": "sku_results.v1",
            "calculation_contract": _CALCULATION_CONTRACT,
            "unit": "EA",
            "items": sorted(o.items, key=lambda x: x["sku_id"]),
        },
        "outcomes": sorted(rows, key=lambda x: x["order_line_id"]),
    }


def _check_rows(w: _World, result: OptionOutcomes) -> None:
    """계약 6절 끝: 엔진 검증기(bundle_verify.py)의 COMPUTED_OUTCOME_ROWS·ORDER_COUNT_MISMATCH 를 입력 T7 과 대조"""
    lines = sum(len(r.lines) for r in w.risks)
    for o in result.options:
        if o.feasibility_status == "NEEDS_CHECK":
            continue
        # FEASIBLE·NOT_FEASIBLE 은 집계가 늘 있다(출력 모델 response_options_ck9, 계약 6절 집계 NULL 규칙)
        if len(o.outcomes) != lines:
            raise ValueError(
                f"{o.option_type} 주문 결과 {len(o.outcomes)}행이 위험 주문 상세 {lines}행과 다릅니다"
            )
        if o.fully_recovered_orders + o.not_recovered_orders != len(w.risks):
            raise ValueError(
                f"{o.option_type} 회복 + 미회복 주문 수가 위험 주문 {len(w.risks)}건과 다릅니다"
            )


def calculate_option_outcomes(
    data: CalculateOptionOutcomesInput, simulate: Simulate | None = None
) -> OptionOutcomes:
    """T10 대응안 결과. T9 의 후보·사전 판정을 받아 대응안마다 계산하고 최종 상태를 정한다(출력은 T9 와 같은 순서)."""
    w = _World(data, simulate or _engine_simulate)
    required = sum(w.items[s].total_prepare_quantity for s in w.k_imp)
    options = []
    for pre in data.option_feasibility.options:
        o = _Option(option_type=pre.option_type)
        reds = {s: _q4(0) for s in w.fc_skus}
        if pre.reason_code == R.MISSING_OPERATING_RULE:
            o.reason_code = (
                "MISSING_OPERATING_RULE"  # 판정 순서 1: 계산하지 않음, SKU 항목도 없음
            )
        elif pre.option_type == O.KEEP_PLAN:
            _keep_plan(w, o, reds)
        elif pre.option_type == O.PRE_DEPARTURE_AIR:
            _pre_departure_air(w, o, pre, reds)
        elif pre.option_type == O.KR_AIR_REPLENISH:
            _kr_air_replenish(w, o, pre, reds)
        else:
            _other_region(w, o, reds)
            t9_unknown = pre.reason_code == R.DONOR_NO_NEXT_INBOUND
            if o.all_unknown != t9_unknown:
                raise ValueError(
                    f"공여 다음 입고 판정이 T9({pre.precheck}/{pre.reason_code})와 다릅니다(T3·T6 입력 확인)"
                )
        _judge(w, o, reds)
        options.append(_option_row(w, o, required))
    result = OptionOutcomes.model_validate({"options": options})
    _check_rows(w, result)
    return result
