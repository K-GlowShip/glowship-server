import uuid
from datetime import date

from app.modules.delay_analysis.application.delay_display import (
    ShipmentLineQuantity,
    build_route,
    calculate_delay_days,
    count_skus,
    pick_representative_line,
)

SKU_A, SKU_B, SKU_C = uuid.uuid7(), uuid.uuid7(), uuid.uuid7()


def _line(line_no: int, sku_id: uuid.UUID, quantity: int) -> ShipmentLineQuantity:
    return ShipmentLineQuantity(
        line_no=line_no, sku_id=sku_id, shipped_quantity=quantity
    )


# --- 경로 ------------------------------------------------------------------------


def test_build_route_drops_state_suffix_of_port():
    assert build_route("KR", "LOS ANGELES, CA") == "KR → LOS ANGELES"
    assert build_route("KR", "NEWARK, NJ") == "KR → NEWARK"


def test_build_route_keeps_port_name_without_state():
    assert build_route("KR", "LOS ANGELES") == "KR → LOS ANGELES"


# --- 대표 상품 -------------------------------------------------------------------


def test_representative_line_has_largest_quantity():
    lines = [_line(1, SKU_A, 100), _line(2, SKU_B, 300), _line(3, SKU_C, 200)]

    assert pick_representative_line(lines).sku_id == SKU_B


def test_representative_line_tie_goes_to_smaller_line_no():
    lines = [_line(3, SKU_C, 300), _line(1, SKU_A, 300), _line(2, SKU_B, 100)]

    assert pick_representative_line(lines).sku_id == SKU_A


def test_representative_line_is_none_without_lines():
    assert pick_representative_line([]) is None


def test_count_skus_counts_distinct_skus():
    lines = [_line(1, SKU_A, 10), _line(2, SKU_A, 20), _line(3, SKU_B, 30)]

    assert count_skus(lines) == 2


# --- 지연 일수 -------------------------------------------------------------------


def test_delay_days_is_expected_minus_planned():
    assert calculate_delay_days(date(2026, 1, 27), date(2026, 1, 28)) == 1
    assert calculate_delay_days(date(2026, 2, 25), date(2026, 3, 3)) == 6


def test_delay_days_is_zero_after_recovery():
    # 회복된 에피소드: 현재 예상일(2/2)이 계획일(2/11)보다 이르면 0
    assert calculate_delay_days(date(2026, 2, 11), date(2026, 2, 2)) == 0
    assert calculate_delay_days(date(2026, 2, 11), date(2026, 2, 11)) == 0
