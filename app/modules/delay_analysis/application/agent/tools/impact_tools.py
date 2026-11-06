import math
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from typing import TYPE_CHECKING, Any, Protocol

from app.modules.delay_analysis.application.agent.tools.impact_schemas import (
    AtRiskOrders,
    CalculateInventoryImpactInput,
    IdentifyAtRiskOrdersInput,
    InventoryImpact,
)
from app.modules.delay_analysis.infrastructure.analysis_enums import SkuScope

if TYPE_CHECKING:
    from app.modules.delay_analysis.infrastructure.engine.engine_input import (
        EngineInput,
    )

"""
    재고 영향 계산 도구 T7·T6 (GLOW-126, docs/agent-flow/tool-contracts.md 4절, docs/analysis/impact-tools.md)
      - 엔진은 pipeline.analyze(snap) 한 번으로 예측 층(fc)·주문 층(ol)·영향 집합(k_imp·k_watch)·결과 분류를 함께 계산한다.
        이 도구는 실행마다 한 번 만든 그 결과(ctx.engine.analysis)를 계약 출력 모델로 **옮기기만** 한다. 다시 계산하지 않는다.
        그래야 도구 출력·판정 서명·대응안이 같은 계산에서 나온다(조회 도구 T1~T8 과 같은 방식, 인터페이스 결정 2·3).
      - 입력(payload)은 계산에 쓰지 않고, 이 실행의 엔진 입력과 맞는지 확인하는 데만 쓴다. 맞지 않으면 다른 실행·다른 순서로
        부른 것이므로 ValueError(call_tool 기준 FAILED, 재시도 없음).
      - 숫자 모양: raw 값은 엔진 common.q4(소수 4자리, half-even)와 같은 규칙의 문자열. min_gap 은 계산할 날이 없으면(inf) null(9절 Q10).
      - 엔진 값을 고치지 않는다.
"""

_Q4 = Decimal("0.0001")


class EngineToolContext(Protocol):
    """도구가 읽는 실행 문맥. 개발 2 ToolContext(engine = EngineInput)가 그대로 맞는다."""

    @property
    def engine(self) -> EngineInput: ...


def _raw4(value: Any) -> str:
    """엔진 common.s4 와 같은 규칙(Decimal(repr(float)) → 소수 4자리 half-even). 부동소수 잡음 -0.0000 은 0.0000 으로"""
    d = value if isinstance(value, Decimal) else Decimal(repr(float(value)))
    q = d.quantize(_Q4, rounding=ROUND_HALF_EVEN)
    return f"{q.copy_abs() if q.is_zero() else q:.4f}"


def _min_gap(value: float) -> str | None:
    """가용 − 누적 수요의 최솟값. 엔진은 계산할 날이 없으면(D ≥ 입고일) inf 를 낸다 → null (9절 Q10)"""
    return None if math.isinf(value) else _raw4(value)


def _check_same_run(engine, delay_context) -> None:
    snap = engine.snap
    expected = (snap["delayed_shipment_id"], snap["D"])
    actual = (delay_context.shipment_id, delay_context.base_date)
    if actual != expected:
        raise ValueError(
            f"입력의 지연 문맥(선적·기준일 {actual})이 이 실행의 엔진 입력 {expected}과 다릅니다"
        )


def _analysis(engine) -> dict[str, Any]:
    analysis = engine.analysis
    if analysis.get("ol") is None or not analysis.get("fc"):
        raise ValueError(
            f"엔진 결과에 예측 층·주문 층이 없습니다(상태 {analysis.get('status')}). NO_DELAY·보류 실행에서는 부르지 않습니다"
        )
    return analysis


def _delay_induced_sku_codes(ol: dict[str, Any]) -> list[str]:
    """엔진 core.delay_induced_skus 와 같은 집합(DELAY_INDUCED 위험 주문의 모든 활성 상세 SKU)"""
    return sorted(
        {
            line["sku"]
            for r in ol["risks"]
            if r["attribution"] == "DELAY_INDUCED"
            for line in r["lines"]
        }
    )


# --- T7 -------------------------------------------------------------------------------------------


def _expected_plan_simulation(ol: dict[str, Any]) -> dict[str, Any]:
    """E 시뮬레이션(ol.simE) 중 T10 이 읽는 값: 위험 주문의 확보일과, 날마다 위험 주문 줄의 보호 수량(계약 4.1절)"""
    sim = ol["simE"]
    codes = {r["order_code"] for r in ol["risks"]}
    return {
        "secured_dates": [
            {"order_code": code, "secured_date": day}
            for code, day in sorted(sim["secured"].items())
            if code in codes
        ],
        "days": [
            {
                "date": day,
                "lines": sorted(
                    (
                        {
                            "order_code": line["order_code"],
                            "line_no": int(line["line_no"]),
                            "protected": int(line["protected"]),
                        }
                        for line in sim["outs"][day]["lines"]
                        if line["order_code"] in codes
                    ),
                    key=lambda x: (x["order_code"], x["line_no"]),
                ),
            }
            for day in sorted(sim["outs"])
        ],
    }


async def identify_at_risk_orders(
    ctx: EngineToolContext, payload: IdentifyAtRiskOrdersInput
) -> AtRiskOrders:
    """T7 위험 주문(엔진 core.order_layer 결과). T6 보다 먼저 부른다(9절 Q3)."""
    engine = ctx.engine
    _check_same_run(engine, payload.delay_context)
    snap = engine.snap
    ol = _analysis(engine)["ol"]
    # 엔진 ol 은 접수 시각을 날짜로 줄여 둔다. 계약은 원래 시각이라 스냅샷의 열린 주문에서 가져온다
    orders = {
        o["order_id"]: o
        for o in snap["restore"][snap["dest"]["warehouse_id"]]["open_orders"]
    }
    risks = []
    for r in ol["risks"]:
        order = orders[r["order_id"]]
        risks.append(
            {
                "order_id": r["order_id"],
                "order_code": r["order_code"],
                "sales_channel": r["channel"],
                "ordered_at": order["ordered_at"],
                "promised_delivery_at": r["promised_at"],
                "risk_attribution": r["attribution"],
                "expected_delivery_date": r["delivery_E"],
                "baseline_expected_delivery_date": r["delivery_P"],
                "expected_beyond_horizon": r["beyond"],
                "lines": [
                    {
                        "order_line_id": line["order_line_id"],
                        "line_no": line["line_no"],
                        "sku_code": line["sku"],
                        "open_quantity": line["open_quantity"],
                        "wait_cause": line["wait_cause"],
                    }
                    for line in sorted(r["lines"], key=lambda x: x["line_no"])
                ],
            }
        )
    return AtRiskOrders.model_validate(
        {
            "risks": sorted(risks, key=lambda x: x["order_code"]),
            "delay_induced_sku_codes": _delay_induced_sku_codes(ol),
            "simulation_horizon_end": ol["h_end"],
            "expected_plan_simulation": _expected_plan_simulation(ol),
        }
    )


# --- T6 -------------------------------------------------------------------------------------------


def _path(side: dict[str, Any]) -> dict[str, Any]:
    """엔진 core.forecast_sku 결과 한쪽(base = P 입고, total = E 입고)"""
    daily: dict[date, float] = side["daily"]
    return {
        "stockout_date": side["stockout_date"],
        "stockout_days": side["stockout_days"],
        "safety_stock_breach_date": side["breach_date"],
        "min_gap": _min_gap(side["min_gap"]),
        "daily_deficits": [
            {"date": day, "deficit": _raw4(daily[day])} for day in sorted(daily)
        ],
    }


async def calculate_inventory_impact(
    ctx: EngineToolContext, payload: CalculateInventoryImpactInput
) -> InventoryImpact:
    """T6 예측 층·영향 집합·결과 분류(엔진 forecast_layer·impact_sets·classify 결과). T7 의 지연 유발 SKU 를 받는다(9절 Q3)."""
    engine = ctx.engine
    _check_same_run(engine, payload.delay_context)
    analysis = _analysis(engine)
    induced = _delay_induced_sku_codes(analysis["ol"])
    if sorted(payload.delay_induced_sku_codes) != induced:
        raise ValueError(
            f"입력의 지연 유발 SKU {sorted(payload.delay_induced_sku_codes)}가 엔진 결과 {induced}와 다릅니다(T7 출력을 넘겼는지 확인)"
        )
    snap = engine.snap
    k_imp, k_watch = set(analysis["k_imp"]), set(analysis["k_watch"])
    items = []
    for code, v in sorted(analysis["fc"].items()):
        items.append(
            {
                "sku_id": snap["sku_ids"][code],
                "sku_code": code,
                "sku_scope": SkuScope.IMPACTED
                if code in k_imp
                else SkuScope.WATCH
                if code in k_watch
                else SkuScope.SHIPMENT_ONLY,
                "raw_baseline_shortage": _raw4(v["raw_baseline"]),
                "raw_total_shortage": _raw4(v["raw_total"]),
                "raw_increment_shortage": _raw4(v["raw_increment"]),
                "baseline_prepare_quantity": v["baseline_prepare"],
                "increment_prepare_quantity": v["increment_prepare"],
                "total_prepare_quantity": v["total_prepare"],
                "safety_stock_quantity": v["total"]["ss"],
                "baseline": _path(v["base"]),
                "total": _path(v["total"]),
            }
        )
    return InventoryImpact.model_validate(
        {
            "items": items,
            "impacted_sku_codes": sorted(k_imp),
            "watch_sku_codes": sorted(k_watch),
            "completed_status": analysis["status"],
            "result_class": analysis["result_class"],
        }
    )
