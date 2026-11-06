from datetime import date
from itertools import product

import pytest

from app.core.config import get_settings
from app.modules.delay_analysis.application.agent.retry import (
    MISSING_REQUIRED_DATA,
    RequiredDataError,
)
from app.modules.delay_analysis.application.agent.tools.option_feasibility import (
    check_option_feasibility,
    option_candidates,
)
from app.modules.delay_analysis.application.agent.tools.option_schemas import (
    CheckOptionFeasibilityInput,
    OptionPrecheck,
    Precheck,
)
from app.modules.delay_analysis.infrastructure.analysis_enums import (
    OptionReasonCode,
    OptionType,
)
from app.modules.delay_analysis.infrastructure.engine.engine_loader import (
    import_engine_module,
    read_manifest,
    resolve_engine_dir,
    verify_engine_dir,
)
from app.modules.shipment.infrastructure.shipment_enums import (
    ShipmentStage,
    TransportMode,
)

"""
    T9 check_option_feasibility (docs/agent-flow/tool-contracts.md 5절)
      - 후보 규칙, 사전 판정 표의 모든 행, 경계값, 예외 처리를 본다.
      - 후보 규칙의 엔진 대조는 GLOWSHIP_ENGINE_DIR 이 있을 때만 돈다.
"""

O, R, P = OptionType, OptionReasonCode, Precheck
BASE_DATE = date(2026, 1, 10)  # D
INBOUND_DATE = date(2026, 1, 20)  # E
SKU_ID = "00000000-0000-4000-8000-00000000000{}"


def _lead_times(**overrides) -> dict:
    return {
        "dest_outbound_handling_days": 1,
        "dest_customer_delivery_days": 2,
        "donor_outbound_handling_days": 1,
        "donor_customer_delivery_days": 3,
        "kr_outbound_handling_days": 1,
        "kr_air_transit_days": 2,
        "dest_inbound_handling_days": 1,
    } | overrides


def _input(mode="OCEAN", stage="AT_SEA", **overrides) -> CheckOptionFeasibilityInput:
    body = {
        "transport_mode": mode,
        "stage": stage,
        "base_date": BASE_DATE,
        "expected_3pl_inbound_date": INBOUND_DATE,
        "planned_kr_release_date": BASE_DATE,
        "lead_times": _lead_times(),
        "impacted_sku_codes": ["SKU-1"],
        "skus": [_sku("SKU-1")],
        "kr_on_hand": [{"sku_code": "SKU-1", "on_hand_quantity": 5}],
        "donor_next_inbound": [
            {"sku_code": "SKU-1", "next_inbound_date": date(2026, 1, 15)}
        ],
    }
    return CheckOptionFeasibilityInput.model_validate(body | overrides)


def _sku(code: str, eligible: bool = True, index: int = 1) -> dict:
    return {
        "sku_id": SKU_ID.format(index),
        "sku_code": code,
        "emergency_air_eligible": eligible,
    }


def _option(data: CheckOptionFeasibilityInput, option_type: O) -> OptionPrecheck:
    options = check_option_feasibility(data).options
    return next(o for o in options if o.option_type == option_type)


def _judged(option: OptionPrecheck) -> tuple:
    return option.precheck, option.reason_code, option.supply_available_date


# --- 후보 규칙 (D-02) ----------------------------------------------------------------------------
KEEP, PRE, KR, OTHER = (
    O.KEEP_PLAN,
    O.PRE_DEPARTURE_AIR,
    O.KR_AIR_REPLENISH,
    O.OTHER_REGION_FULFILL,
)
CANDIDATES = {
    ("OCEAN", "PRE_DEPARTURE"): [KEEP, PRE, OTHER],
    ("OCEAN", "AT_SEA"): [KEEP, KR, OTHER],
    ("OCEAN", "AT_PORT"): [KEEP, KR, OTHER],
    ("OCEAN", "IN_AIR"): [KEEP, OTHER],
    ("OCEAN", "RECEIVED"): [KEEP, OTHER],
    ("AIR", "PRE_DEPARTURE"): [KEEP, KR, OTHER],
    ("AIR", "IN_AIR"): [KEEP, KR, OTHER],
    ("AIR", "AT_SEA"): [KEEP, OTHER],
    ("AIR", "AT_PORT"): [KEEP, OTHER],
    ("AIR", "RECEIVED"): [KEEP, OTHER],
}


def test_candidate_table_covers_every_mode_and_stage():
    assert set(CANDIDATES) == {
        (mode.value, stage.value)
        for mode, stage in product(TransportMode, ShipmentStage)
    }


@pytest.mark.parametrize(("key", "expected"), CANDIDATES.items())
def test_candidates_follow_the_rule(key, expected):
    mode, stage = key
    options = check_option_feasibility(_input(mode, stage)).options
    assert [o.option_type for o in options] == expected


def test_keep_plan_calculates_on_delayed_inbound_date():
    assert _judged(_option(_input(), KEEP)) == (P.CALCULATE, None, INBOUND_DATE)


# --- PRE_DEPARTURE_AIR ---------------------------------------------------------------------------
def _pre(**overrides) -> OptionPrecheck:
    return _option(_input("OCEAN", "PRE_DEPARTURE", **overrides), PRE)


@pytest.mark.parametrize(
    "field",
    [
        "kr_outbound_handling_days",
        "kr_air_transit_days",
        "dest_inbound_handling_days",
    ],
)
def test_air_options_need_check_without_a_lead_time(field):
    lead_times = _lead_times(**{field: None})
    expected = (P.NEEDS_CHECK, R.MISSING_OPERATING_RULE, None)
    assert _judged(_pre(lead_times=lead_times)) == expected
    assert _judged(_option(_input(lead_times=lead_times), KR)) == expected


def test_pre_departure_air_starts_from_the_later_of_base_and_release_date():
    earlier = _pre(planned_kr_release_date=date(2026, 1, 5))
    later = _pre(planned_kr_release_date=date(2026, 1, 12))
    assert _judged(earlier) == (P.CALCULATE, None, date(2026, 1, 14))
    assert _judged(later) == (P.CALCULATE, None, date(2026, 1, 16))


def test_pre_departure_air_boundary_at_delayed_inbound_date():
    one_day_before = _pre(planned_kr_release_date=date(2026, 1, 15))
    same_day = _pre(planned_kr_release_date=date(2026, 1, 16))
    assert _judged(one_day_before) == (P.CALCULATE, None, date(2026, 1, 19))
    assert _judged(same_day) == (
        P.NO_SUPPLY,
        R.ARRIVES_AFTER_DELAYED_SHIPMENT,
        INBOUND_DATE,
    )


# --- KR_AIR_REPLENISH ----------------------------------------------------------------------------
def test_kr_air_replenish_calculates_from_base_date():
    assert _judged(_option(_input(), KR)) == (P.CALCULATE, None, date(2026, 1, 14))


def test_kr_air_replenish_boundary_at_delayed_inbound_date():
    one_day_before = _input(expected_3pl_inbound_date=date(2026, 1, 15))
    same_day = _input(expected_3pl_inbound_date=date(2026, 1, 14))
    assert _option(one_day_before, KR).precheck == P.CALCULATE
    assert _judged(_option(same_day, KR)) == (
        P.NO_SUPPLY,
        R.ARRIVES_AFTER_DELAYED_SHIPMENT,
        date(2026, 1, 14),
    )


def test_late_arrival_comes_before_missing_kr_stock():
    data = _input(expected_3pl_inbound_date=date(2026, 1, 14), kr_on_hand=[])
    assert _option(data, KR).reason_code == R.ARRIVES_AFTER_DELAYED_SHIPMENT


@pytest.mark.parametrize(
    "overrides",
    [
        {"skus": [_sku("SKU-1", eligible=False)]},  # 긴급 항공 가능 SKU 없음
        {"kr_on_hand": [{"sku_code": "SKU-1", "on_hand_quantity": 0}]},
        {"kr_on_hand": [{"sku_code": "SKU-1", "on_hand_quantity": -1}]},
        {"kr_on_hand": []},  # 행이 없으면 보유 0
        {"impacted_sku_codes": []},
    ],
)
def test_kr_air_replenish_without_eligible_stock(overrides):
    assert _judged(_option(_input(**overrides), KR)) == (
        P.NO_SUPPLY,
        R.NO_KR_ELIGIBLE_STOCK,
        date(2026, 1, 14),
    )


def test_eligible_skus_are_sorted_and_keep_zero_stock():
    data = _input(
        impacted_sku_codes=["SKU-3", "SKU-1", "SKU-2"],
        skus=[_sku("SKU-1"), _sku("SKU-2", False, 2), _sku("SKU-3", index=3)],
        kr_on_hand=[{"sku_code": "SKU-3", "on_hand_quantity": 1}],
    )
    option = _option(data, KR)
    assert option.precheck == P.CALCULATE
    assert option.eligible_sku_codes == ["SKU-1", "SKU-3"]


def test_eligible_skus_stay_filled_without_a_lead_time():
    data = _input(lead_times=_lead_times(kr_air_transit_days=None))
    assert _option(data, KR).eligible_sku_codes == ["SKU-1"]


def test_eligible_skus_are_only_on_kr_air_replenish():
    options = check_option_feasibility(_input()).options
    assert [o.eligible_sku_codes for o in options if o.option_type != KR] == [[], []]


# --- OTHER_REGION_FULFILL ------------------------------------------------------------------------
@pytest.mark.parametrize(
    "field", ["donor_outbound_handling_days", "donor_customer_delivery_days"]
)
def test_other_region_needs_check_without_a_lead_time(field):
    data = _input(lead_times=_lead_times(**{field: None}))
    assert _judged(_option(data, OTHER)) == (
        P.NEEDS_CHECK,
        R.MISSING_OPERATING_RULE,
        None,
    )


@pytest.mark.parametrize(
    "rows",
    [
        [{"sku_code": "SKU-1", "next_inbound_date": None}],
        [],  # 행이 없으면 다음 입고 없음과 같음
    ],
)
def test_other_region_needs_check_when_no_impacted_sku_has_next_inbound(rows):
    assert _judged(_option(_input(donor_next_inbound=rows), OTHER)) == (
        P.NEEDS_CHECK,
        R.DONOR_NO_NEXT_INBOUND,
        BASE_DATE,
    )


def test_other_region_calculates_when_one_impacted_sku_has_next_inbound():
    data = _input(
        impacted_sku_codes=["SKU-1", "SKU-2"],
        skus=[_sku("SKU-1"), _sku("SKU-2", index=2)],
        donor_next_inbound=[
            {"sku_code": "SKU-1", "next_inbound_date": None},
            {"sku_code": "SKU-2", "next_inbound_date": date(2026, 1, 15)},
        ],
    )
    assert _judged(_option(data, OTHER)) == (P.CALCULATE, None, BASE_DATE)


def test_other_region_calculates_without_impacted_skus():
    data = _input(impacted_sku_codes=[], donor_next_inbound=[])
    assert _judged(_option(data, OTHER)) == (P.CALCULATE, None, BASE_DATE)


# --- 공통 ----------------------------------------------------------------------------------------
def test_zero_lead_time_is_a_value():
    data = _input(
        lead_times=_lead_times(
            kr_outbound_handling_days=0,
            kr_air_transit_days=0,
            dest_inbound_handling_days=0,
            donor_outbound_handling_days=0,
            donor_customer_delivery_days=0,
        )
    )
    assert _judged(_option(data, KR)) == (P.CALCULATE, None, BASE_DATE)
    assert _judged(_option(data, OTHER)) == (P.CALCULATE, None, BASE_DATE)


@pytest.mark.parametrize(
    ("mode", "stage"), [("OCEAN", "AT_SEA"), ("OCEAN", "RECEIVED")]
)
def test_impacted_sku_without_attribute_holds_the_run(mode, stage):
    with pytest.raises(RequiredDataError) as error:
        check_option_feasibility(_input(mode, stage, skus=[]))
    assert error.value.code == MISSING_REQUIRED_DATA


# --- 엔진 대조 (GLOWSHIP_ENGINE_DIR 이 있을 때만) ------------------------------------------------
def test_candidates_match_engine():
    configured = get_settings().glowship_engine_dir
    if configured is None:
        pytest.skip("GLOWSHIP_ENGINE_DIR 이 없어 엔진 대조를 건너뜀")
    engine_dir = resolve_engine_dir(configured)
    verify_engine_dir(engine_dir, read_manifest())
    engine = import_engine_module(engine_dir, "options")

    for mode, stage in product(TransportMode, ShipmentStage):
        ours = [o.value for o in option_candidates(mode, stage)]
        assert ours == engine.candidates(mode.value, stage.value), (mode, stage)
