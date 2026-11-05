import math
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Decimal
from enum import StrEnum

"""
    거점별 재고 현황 판정 규칙 (API 명세 v6.2 4.4 GET /inventory-status, D9·C22) — 순수 함수, DB 없음
      - 분석 엔진(glowship_engine core.forecast_sku)과 같은 정의다. 다만 입고 예정은 반영하지 않는다(C22: 참고값).
      - 기준일 D 부터 하루씩 가용 수량에서 하루 판매량 μ 를 뺀다. D 날의 판매도 빼므로 k 번째 날(D + k)의 남은 수량은
        available − μ × (k + 1) 이다 (엔진도 D 날의 μ 를 먼저 누적한다).
          안전재고         = ceil(μ × safety_stock_days)
          안전재고 하회일   = 남은 수량이 안전재고보다 처음 작아지는 날
          품절일           = 남은 수량이 0 이하가 되는 첫날
        남은 수량은 소수 4자리(half-even)로 비교하고, 올림도 그 값에서 한다 (엔진 common.q4·ceil_i, DB numeric(12,4)).
      - μ 가 0 이면 줄지 않으므로 두 날짜 모두 None 이고 OK 다.
      - μ 가 아주 작아 날짜가 date.max(9999-12-31)를 넘으면 그 날짜는 None 이다(C22). 하회일이 None 이면 OK 다.
      - status: 하회일 ≤ D → RISK, D < 하회일 ≤ D + safety_stock_days → CAUTION, 그 밖(하회일 없음 포함) → OK
"""

_Q4 = Decimal("0.0001")


class InventoryStatus(StrEnum):
    OK = "OK"
    CAUTION = "CAUTION"
    RISK = "RISK"


@dataclass(frozen=True)
class InventoryOutlook:
    safety_stock_quantity: int
    safety_stock_breach_date: date | None
    stockout_date: date | None
    status: InventoryStatus


def q4(value: float | Decimal) -> Decimal:
    """소수 4자리 half-even (엔진 common.q4 와 같음: float 는 repr 로 Decimal 로 바꾼 뒤 고정)"""
    decimal = value if isinstance(value, Decimal) else Decimal(repr(float(value)))
    return decimal.quantize(_Q4, rounding=ROUND_HALF_EVEN)


def ceil_q4(value: float) -> int:
    """소수 4자리로 고정한 뒤 올림 (엔진 common.ceil_i)"""
    return int(q4(value).to_integral_value(rounding=ROUND_CEILING))


def average_daily_sales(total_demand: int, sales_basis_days: int) -> float:
    """하루 판매량 μ = 기간 유효 주문 수요 합 ÷ 기간 일수 (D9)"""
    return total_demand / sales_basis_days


def _remaining(available: int, daily_sales: float, offset: int) -> Decimal:
    """D + offset 날 판매까지 뺀 남은 수량 (소수 4자리)"""
    return q4(available - daily_sales * (offset + 1))


def _first_offset(available: int, daily_sales: float, below) -> int:
    """below(남은 수량)가 처음 참이 되는 offset. 남은 수량이 줄어들기만 하므로 이분 탐색한다."""
    high = max(0, math.ceil(available / daily_sales))
    while not below(_remaining(available, daily_sales, high)):
        high = high * 2 + 1
    low = 0
    while low < high:
        middle = (low + high) // 2
        if below(_remaining(available, daily_sales, middle)):
            high = middle
        else:
            low = middle + 1
    return low


def _date_after(basis_date: date, offset: int) -> date | None:
    """basis_date + offset 일. date.max 를 넘어 날짜로 나타낼 수 없으면 None (C22)"""
    if offset > (date.max - basis_date).days:
        return None
    return basis_date + timedelta(days=offset)


def judge_inventory(
    *,
    available_quantity: int,
    daily_sales: float,
    safety_stock_days: int,
    basis_date: date,
) -> InventoryOutlook:
    """가용 수량·하루 판매량·안전재고 일수로 안전재고 하회일·품절일·상태를 정한다 (입고 예정 미반영)."""
    safety_stock = ceil_q4(daily_sales * safety_stock_days)
    if daily_sales <= 0:
        return InventoryOutlook(
            safety_stock_quantity=safety_stock,
            safety_stock_breach_date=None,
            stockout_date=None,
            status=InventoryStatus.OK,
        )
    breach_offset = _first_offset(
        available_quantity, daily_sales, lambda rest: rest < safety_stock
    )
    stockout_offset = _first_offset(
        available_quantity, daily_sales, lambda rest: rest <= 0
    )
    breach_date = _date_after(basis_date, breach_offset)
    if breach_date is None:
        status = InventoryStatus.OK
    elif breach_offset == 0:
        status = InventoryStatus.RISK
    elif breach_offset <= safety_stock_days:
        status = InventoryStatus.CAUTION
    else:
        status = InventoryStatus.OK
    return InventoryOutlook(
        safety_stock_quantity=safety_stock,
        safety_stock_breach_date=breach_date,
        stockout_date=_date_after(basis_date, stockout_offset),
        status=status,
    )
