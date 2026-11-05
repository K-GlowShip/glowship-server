import random
from datetime import date, timedelta

import pytest

from app.modules.inventory.application.inventory_status_rules import (
    InventoryStatus,
    average_daily_sales,
    ceil_q4,
    judge_inventory,
    q4,
)

"""
    재고 현황 판정 규칙 단위 테스트 (명세 4.4 GET /inventory-status, D9·C22)
      - 손으로 센 경우와, 분석 엔진 core.forecast_sku 의 하루씩 누적하는 반복을 그대로 옮긴 비교 기준(_engine_dates)으로 확인한다.
"""

D = date(2026, 7, 1)


def _engine_dates(available: int, daily_sales: float, safety_days: int):
    """엔진 forecast_sku 의 날짜 판정을 입고 없이 옮긴 것: 날마다 μ 를 누적하고 소수 4자리로 비교한다."""
    safety_stock = ceil_q4(daily_sales * safety_days)
    cumulative, breach, stockout, day = 0.0, None, None, D
    while breach is None or stockout is None:
        cumulative += daily_sales
        gap = q4(available - cumulative)
        if gap <= 0 and stockout is None:
            stockout = day
        if gap < safety_stock and breach is None:
            breach = day
        day += timedelta(days=1)
    return safety_stock, breach, stockout


def test_하루_판매량은_기간_수요_합을_기간_일수로_나눈다():
    assert average_daily_sales(280, 28) == 10.0
    assert average_daily_sales(0, 28) == 0.0


def test_손으로_센_경우_안전재고_하회일과_품절일():
    """가용 100, 하루 10, 안전재고 7일 → 안전재고 70.
    D 날 판매 뒤 90, D+1 80, D+2 70(70 < 70 아님), D+3 60 < 70 → 하회일 D+3. 0 이하는 D+9 (100 − 10×10 = 0)."""
    outlook = judge_inventory(
        available_quantity=100, daily_sales=10.0, safety_stock_days=7, basis_date=D
    )

    assert outlook.safety_stock_quantity == 70
    assert outlook.safety_stock_breach_date == D + timedelta(days=3)
    assert outlook.stockout_date == D + timedelta(days=9)
    assert outlook.status == InventoryStatus.CAUTION


def test_기준일에_이미_안전재고_아래면_RISK():
    outlook = judge_inventory(
        available_quantity=50, daily_sales=10.0, safety_stock_days=7, basis_date=D
    )

    assert outlook.safety_stock_breach_date == D
    assert outlook.status == InventoryStatus.RISK


def test_하회일이_안전재고_일수를_넘으면_OK():
    """가용 1000, 하루 10, 7일 → 1000 − 10×(k+1) < 70 은 k = 93 → 하회일 D+93, D+7 뒤라 OK"""
    outlook = judge_inventory(
        available_quantity=1000, daily_sales=10.0, safety_stock_days=7, basis_date=D
    )

    assert outlook.safety_stock_breach_date == D + timedelta(days=93)
    assert outlook.status == InventoryStatus.OK


def test_하회일이_정확히_안전재고_일수째면_CAUTION():
    """가용 140, 하루 10, 7일(안전재고 70): D+6 날 남은 수량 70(아직 아님), D+7 날 60 < 70
    → 하회일 = D+7 = 경계(D + safety_stock_days) → CAUTION"""
    outlook = judge_inventory(
        available_quantity=140, daily_sales=10.0, safety_stock_days=7, basis_date=D
    )

    assert outlook.safety_stock_breach_date == D + timedelta(days=7)
    assert outlook.status == InventoryStatus.CAUTION


def test_판매량이_0이면_날짜_없이_OK():
    outlook = judge_inventory(
        available_quantity=0, daily_sales=0.0, safety_stock_days=7, basis_date=D
    )

    assert (outlook.safety_stock_breach_date, outlook.stockout_date) == (None, None)
    assert (outlook.safety_stock_quantity, outlook.status) == (0, InventoryStatus.OK)


def test_가용_0이고_판매가_있으면_기준일에_품절이고_RISK():
    outlook = judge_inventory(
        available_quantity=0, daily_sales=0.5, safety_stock_days=7, basis_date=D
    )

    assert outlook.stockout_date == D
    assert outlook.safety_stock_breach_date == D
    assert outlook.status == InventoryStatus.RISK


@pytest.mark.parametrize(
    ("available", "daily_sales"),
    [(130_000, average_daily_sales(1, 28)), (10_000_000, 0.5)],
)
def test_날짜로_나타낼_수_없는_먼_날은_None이고_OK(available, daily_sales):
    """판매량이 아주 작고 재고가 크면 날짜가 date.max 를 넘는다 → 그 날짜는 None, 상태 OK (C22, PR #23 리뷰)"""
    outlook = judge_inventory(
        available_quantity=available,
        daily_sales=daily_sales,
        safety_stock_days=14,
        basis_date=D,
    )

    assert (outlook.safety_stock_breach_date, outlook.stockout_date) == (None, None)
    assert outlook.status == InventoryStatus.OK


def test_하회일은_나타낼_수_있고_품절일만_넘으면_품절일만_None():
    """μ = 1, 안전재고 20, 가용 = (date.max − D) + 10: 하회일은 offset span − 10(나타낼 수 있음),
    품절일은 offset span + 9(date.max 뒤) → 품절일만 None. 상태는 하회일로 정하므로 OK."""
    span = (date.max - D).days
    outlook = judge_inventory(
        available_quantity=span + 10,
        daily_sales=1.0,
        safety_stock_days=20,
        basis_date=D,
    )

    assert outlook.safety_stock_breach_date == D + timedelta(days=span - 10)
    assert outlook.stockout_date is None
    assert outlook.status == InventoryStatus.OK


def test_안전재고는_소수_4자리로_고정한_뒤_올린다():
    """μ = 1/3, 7일 → 2.3333…→ 3. μ × 일수가 float 잡음으로 정수를 살짝 넘어도 올리지 않는다."""
    assert ceil_q4((1 / 3) * 7) == 3
    assert ceil_q4(0.1 * 3 * 10) == 3  # 3.0000000000000004 → 3


@pytest.mark.parametrize("seed", range(5))
def test_엔진의_하루씩_누적_반복과_같은_날짜를_낸다(seed):
    rng = random.Random(seed)
    for _ in range(200):
        available = rng.randint(0, 3000)
        daily_sales = average_daily_sales(rng.randint(1, 2000), 28)
        safety_days = rng.choice([3, 7, 14])

        outlook = judge_inventory(
            available_quantity=available,
            daily_sales=daily_sales,
            safety_stock_days=safety_days,
            basis_date=D,
        )

        assert (
            outlook.safety_stock_quantity,
            outlook.safety_stock_breach_date,
            outlook.stockout_date,
        ) == _engine_dates(available, daily_sales, safety_days), (
            available,
            daily_sales,
            safety_days,
        )
