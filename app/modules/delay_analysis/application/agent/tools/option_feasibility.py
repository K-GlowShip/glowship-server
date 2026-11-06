from datetime import date, timedelta

from app.modules.delay_analysis.application.agent.retry import (
    MISSING_REQUIRED_DATA,
    RequiredDataError,
)
from app.modules.delay_analysis.application.agent.tools.option_schemas import (
    CheckOptionFeasibilityInput,
    OptionFeasibility,
    OptionPrecheck,
    Precheck,
)
from app.modules.delay_analysis.infrastructure.analysis_enums import (
    OptionReasonCode,
    OptionType,
)
from app.modules.shipment.infrastructure.shipment_enums import (
    ShipmentStage,
    TransportMode,
)

"""
    T9 check_option_feasibility (docs/agent-flow/tool-contracts.md 5절, 엔진 options.candidates·evaluate 114~168행)
      - 입력만으로 계산하는 순수 함수다. 후보와 사전 판정만 내고, 최종 feasibility_status 는 T10 이 정한다(9절 Q2).
      - 영향 SKU 의 긴급 항공 가능 여부를 알 수 없으면 실행 전체를 보류한다(9절 Q7·Q13).
"""

O, R = OptionType, OptionReasonCode


def option_candidates(mode: TransportMode, stage: ShipmentStage) -> list[OptionType]:
    """후보 규칙(D-02). 고정 순서로 돌려주고, 후보가 아닌 대응안은 넣지 않는다."""
    options = [O.KEEP_PLAN]
    if mode == TransportMode.OCEAN and stage == ShipmentStage.PRE_DEPARTURE:
        options.append(O.PRE_DEPARTURE_AIR)
    if (
        mode == TransportMode.OCEAN
        and stage in (ShipmentStage.AT_SEA, ShipmentStage.AT_PORT)
    ) or (
        mode == TransportMode.AIR
        and stage in (ShipmentStage.PRE_DEPARTURE, ShipmentStage.IN_AIR)
    ):
        options.append(O.KR_AIR_REPLENISH)
    options.append(O.OTHER_REGION_FULFILL)
    return options


def _kr_air_days(data: CheckOptionFeasibilityInput) -> int | None:
    """한국 출고 처리 + 항공 + 도착 입고 처리. 0일은 정상값이고, 하나라도 없으면 None."""
    lead_times = data.lead_times
    days = (
        lead_times.kr_outbound_handling_days,
        lead_times.kr_air_transit_days,
        lead_times.dest_inbound_handling_days,
    )
    return None if None in days else sum(days)


def _missing_rule(option_type: OptionType) -> OptionPrecheck:
    return OptionPrecheck(
        option_type=option_type,
        precheck=Precheck.NEEDS_CHECK,
        reason_code=R.MISSING_OPERATING_RULE,
        supply_available_date=None,
        eligible_sku_codes=[],
    )


def _keep_plan(data: CheckOptionFeasibilityInput) -> OptionPrecheck:
    return OptionPrecheck(
        option_type=O.KEEP_PLAN,
        precheck=Precheck.CALCULATE,
        reason_code=None,
        supply_available_date=data.expected_3pl_inbound_date,
        eligible_sku_codes=[],
    )


def _pre_departure_air(data: CheckOptionFeasibilityInput) -> OptionPrecheck:
    days = _kr_air_days(data)
    if days is None:
        return _missing_rule(O.PRE_DEPARTURE_AIR)
    supply_date = max(data.base_date, data.planned_kr_release_date) + timedelta(days)
    arrives_late = supply_date >= data.expected_3pl_inbound_date
    return OptionPrecheck(
        option_type=O.PRE_DEPARTURE_AIR,
        precheck=Precheck.NO_SUPPLY if arrives_late else Precheck.CALCULATE,
        reason_code=R.ARRIVES_AFTER_DELAYED_SHIPMENT if arrives_late else None,
        supply_available_date=supply_date,
        eligible_sku_codes=[],
    )


def _kr_air_replenish(
    data: CheckOptionFeasibilityInput, eligible_sku_codes: list[str]
) -> OptionPrecheck:
    days = _kr_air_days(data)
    if days is None:
        return _missing_rule(O.KR_AIR_REPLENISH).model_copy(
            update={"eligible_sku_codes": eligible_sku_codes}
        )
    supply_date = data.base_date + timedelta(days)
    on_hand = {row.sku_code: row.on_hand_quantity for row in data.kr_on_hand}
    reason_code = None
    if supply_date >= data.expected_3pl_inbound_date:
        reason_code = R.ARRIVES_AFTER_DELAYED_SHIPMENT
    elif all(on_hand.get(code, 0) <= 0 for code in eligible_sku_codes):
        reason_code = R.NO_KR_ELIGIBLE_STOCK
    return OptionPrecheck(
        option_type=O.KR_AIR_REPLENISH,
        precheck=Precheck.NO_SUPPLY if reason_code else Precheck.CALCULATE,
        reason_code=reason_code,
        supply_available_date=supply_date,
        eligible_sku_codes=eligible_sku_codes,
    )


def _other_region_fulfill(data: CheckOptionFeasibilityInput) -> OptionPrecheck:
    lead_times = data.lead_times
    if (
        lead_times.donor_outbound_handling_days is None
        or lead_times.donor_customer_delivery_days is None
    ):
        return _missing_rule(O.OTHER_REGION_FULFILL)
    next_inbound: dict[str, date | None] = {
        row.sku_code: row.next_inbound_date for row in data.donor_next_inbound
    }
    # 영향 SKU 가 없으면 all() 이 참이 되므로 먼저 막는다(엔진 `k_imp and all(...)`)
    all_unknown = bool(data.impacted_sku_codes) and all(
        next_inbound.get(code) is None for code in data.impacted_sku_codes
    )
    return OptionPrecheck(
        option_type=O.OTHER_REGION_FULFILL,
        precheck=Precheck.NEEDS_CHECK if all_unknown else Precheck.CALCULATE,
        reason_code=R.DONOR_NO_NEXT_INBOUND if all_unknown else None,
        supply_available_date=data.base_date,
        eligible_sku_codes=[],
    )


def _eligible_sku_codes(data: CheckOptionFeasibilityInput) -> list[str]:
    """긴급 항공 가능한 영향 SKU. 한국 재고는 보지 않는다(재고 0 SKU 도 포함, 계약 5절)."""
    eligible = {sku.sku_code: sku.emergency_air_eligible for sku in data.skus}
    unknown = sorted(set(data.impacted_sku_codes) - eligible.keys())
    if unknown:
        raise RequiredDataError(
            MISSING_REQUIRED_DATA, f"영향 SKU 의 상품 정보가 없습니다: {unknown}"
        )
    return sorted(code for code in data.impacted_sku_codes if eligible[code])


def check_option_feasibility(data: CheckOptionFeasibilityInput) -> OptionFeasibility:
    eligible_sku_codes = _eligible_sku_codes(data)
    options = []
    for option_type in option_candidates(data.transport_mode, data.stage):
        if option_type == O.KEEP_PLAN:
            options.append(_keep_plan(data))
        elif option_type == O.PRE_DEPARTURE_AIR:
            options.append(_pre_departure_air(data))
        elif option_type == O.KR_AIR_REPLENISH:
            options.append(_kr_air_replenish(data, eligible_sku_codes))
        else:
            options.append(_other_region_fulfill(data))
    return OptionFeasibility(options=options)
