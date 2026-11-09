from app.modules.delay_analysis.application.agent.tools.lookup_schemas import (
    DonorNextInbound,
    KrOnHand,
    LeadTimes,
    SkuAttribute,
)
from app.modules.delay_analysis.application.agent.tools.option_schemas import (
    CheckOptionFeasibilityInput,
    CompareOptionsInput,
    OptionComparisonInput,
    OptionFeasibility,
    OptionRanking,
)

"""
    엔진 snap·analysis → 서버 도구 입력 (GLOW-58 1단계, 시험 전용)
      - T9 입력: 엔진 snap 의 같은 뜻 키를 계약 이름(tool-contracts.md 5절 입력 표)으로 옮긴다.
      - T11 입력: 엔진 analysis["options"] 에서 순위에 쓰는 값만 뽑는다(7절).
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
