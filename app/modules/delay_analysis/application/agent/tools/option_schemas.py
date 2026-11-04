from enum import StrEnum
from typing import Self

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from app.modules.delay_analysis.application.agent.tools.common import (
    ShipmentStage,
    ToolDate,
    ToolId,
    ToolModel,
    TransportMode,
    UtcTimestamp,
)
from app.modules.delay_analysis.application.agent.tools.impact_schemas import (
    AtRiskOrders,
    InventoryImpact,
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
from app.modules.delay_analysis.infrastructure.analysis_enums import (
    FeasibilityStatus,
    LineSecurementStatus,
    NotRecoveredReason,
    OptionReasonCode,
    OptionType,
    OrderOutcome,
    SupplySource,
)
from app.modules.delay_analysis.infrastructure.stored_json_schemas import SkuResults

"""
    대응안 도구 T9·T10·T11 (docs/agent-flow/tool-contracts.md 5~7절, A 구현)
      - T9 는 후보와 사전 판정(precheck)만 내고, 최종 feasibility_status 는 T10 이 정한다(9절 Q2).
      - 사유 집합은 6.1절 표 A(대응안)·표 B(주문 미회복)를, 값 조합은 DDL response_options·option_order_outcomes CHECK 를 따른다.
      - 대표 사유가 DONOR_NO_NEXT_INBOUND 인 경우는 NEEDS_CHECK 다(9절 Q9). DDL ck6 이 NOT_FEASIBLE 과의 조합을 막는다.
"""

O, F, R = OptionType, FeasibilityStatus, OptionReasonCode
OPTION_ORDER = [
    O.KEEP_PLAN,
    O.PRE_DEPARTURE_AIR,
    O.KR_AIR_REPLENISH,
    O.OTHER_REGION_FULFILL,
]


def _check_option_order(option_types: list[OptionType]) -> None:
    """대응안은 고정 순서로 한 번씩만. KEEP_PLAN 과 OTHER_REGION_FULFILL 은 항상 후보다(5절 후보 규칙)."""
    if option_types != [o for o in OPTION_ORDER if o in option_types]:
        raise ValueError(
            "대응안은 KEEP_PLAN → PRE_DEPARTURE_AIR → KR_AIR_REPLENISH → OTHER_REGION_FULFILL 순서로 한 번씩만 둡니다"
        )
    if O.KEEP_PLAN not in option_types or O.OTHER_REGION_FULFILL not in option_types:
        raise ValueError("KEEP_PLAN 과 OTHER_REGION_FULFILL 은 항상 후보입니다")


# --- T9 check_option_feasibility -----------------------------------------------------------------


class Precheck(StrEnum):
    """T9 사전 판정. 이 계약에서 새로 정한 값이고 DB에 저장하지 않는다(9절 Q2)."""

    CALCULATE = "CALCULATE"  # T10 이 계산
    NO_SUPPLY = "NO_SUPPLY"  # 공급 없음. T10 이 "계산 완료 NOT_FEASIBLE"로 처리
    NEEDS_CHECK = "NEEDS_CHECK"  # T10 이 계산하지 않음, 또는 SKU 항목만


# 5절 사전 판정 표: 대응안 → 사전 판정 → 허용 사유
PRECHECK_REASONS: dict[OptionType, dict[Precheck, set[OptionReasonCode | None]]] = {
    O.KEEP_PLAN: {Precheck.CALCULATE: {None}},
    O.PRE_DEPARTURE_AIR: {
        Precheck.CALCULATE: {None},
        Precheck.NEEDS_CHECK: {R.MISSING_OPERATING_RULE},
        Precheck.NO_SUPPLY: {R.ARRIVES_AFTER_DELAYED_SHIPMENT},
    },
    O.KR_AIR_REPLENISH: {
        Precheck.CALCULATE: {None},
        Precheck.NEEDS_CHECK: {R.MISSING_OPERATING_RULE},
        Precheck.NO_SUPPLY: {R.ARRIVES_AFTER_DELAYED_SHIPMENT, R.NO_KR_ELIGIBLE_STOCK},
    },
    O.OTHER_REGION_FULFILL: {
        Precheck.CALCULATE: {None},
        Precheck.NEEDS_CHECK: {R.MISSING_OPERATING_RULE, R.DONOR_NO_NEXT_INBOUND},
    },
}


class CheckOptionFeasibilityInput(ToolModel):
    """T9 입력 (5절 입력 표)"""

    transport_mode: TransportMode  # T1
    stage: ShipmentStage  # T1
    base_date: ToolDate  # T1, D
    expected_3pl_inbound_date: ToolDate  # T1, E
    planned_kr_release_date: ToolDate  # T1
    lead_times: LeadTimes  # T8
    impacted_sku_codes: list[StrictStr]  # T6
    skus: list[SkuAttribute]  # T8 (emergency_air_eligible)
    kr_on_hand: list[KrOnHand]  # T2
    donor_next_inbound: list[DonorNextInbound]  # T3


class OptionPrecheck(ToolModel):
    option_type: OptionType
    precheck: Precheck
    reason_code: OptionReasonCode | None  # NO_SUPPLY·NEEDS_CHECK 일 때의 사유
    supply_available_date: ToolDate | None  # 공급 가능일 S. 리드타임이 없으면 null
    eligible_sku_codes: list[
        StrictStr
    ]  # KR_AIR_REPLENISH 만 값이 있고, 나머지는 빈 배열

    @model_validator(mode="after")
    def _match_precheck_table(self) -> Self:
        allowed = PRECHECK_REASONS[self.option_type].get(self.precheck)
        if allowed is None or self.reason_code not in allowed:
            raise ValueError(
                f"{self.option_type} 의 사전 판정 {self.precheck} / {self.reason_code} 은 5절 표에 없습니다"
            )
        if (
            self.reason_code == R.MISSING_OPERATING_RULE
            and self.supply_available_date is not None
        ):
            raise ValueError("리드타임이 없으면 공급 가능일은 null 입니다")
        if (
            self.reason_code != R.MISSING_OPERATING_RULE
            and self.supply_available_date is None
        ):
            raise ValueError("리드타임이 있으면 공급 가능일이 있어야 합니다")
        if self.option_type != O.KR_AIR_REPLENISH and self.eligible_sku_codes:
            raise ValueError("eligible_sku_codes 는 KR_AIR_REPLENISH 에만 둡니다")
        return self


class OptionFeasibility(ToolModel):
    """T9 출력: 후보 대응안만, 고정 순서"""

    options: list[OptionPrecheck]

    @model_validator(mode="after")
    def _fixed_order(self) -> Self:
        _check_option_order([o.option_type for o in self.options])
        return self


# --- T10 calculate_option_outcomes ---------------------------------------------------------------

# 6.1절 표 A: 대응안 → 최종 상태 → 허용 사유 (FEASIBLE 은 사유 없음, response_options_ck5)
OPTION_REASONS: dict[
    OptionType, dict[FeasibilityStatus, set[OptionReasonCode | None]]
] = {
    O.KEEP_PLAN: {F.FEASIBLE: {None}},
    O.PRE_DEPARTURE_AIR: {
        F.FEASIBLE: {None},
        F.NEEDS_CHECK: {R.MISSING_OPERATING_RULE},
        F.NOT_FEASIBLE: {
            R.ARRIVES_AFTER_DELAYED_SHIPMENT,
            R.FORECAST_EFFECT_BELOW_ONE_UNIT,
            R.NO_RECOVERY_EFFECT,
        },
    },
    O.KR_AIR_REPLENISH: {
        F.FEASIBLE: {None},
        F.NEEDS_CHECK: {R.MISSING_OPERATING_RULE},
        F.NOT_FEASIBLE: {
            R.ARRIVES_AFTER_DELAYED_SHIPMENT,
            R.NO_KR_ELIGIBLE_STOCK,
            R.FORECAST_EFFECT_BELOW_ONE_UNIT,
            R.NO_RECOVERY_EFFECT,
        },
    },
    O.OTHER_REGION_FULFILL: {
        F.FEASIBLE: {None},
        F.NEEDS_CHECK: {R.MISSING_OPERATING_RULE, R.DONOR_NO_NEXT_INBOUND},
        F.NOT_FEASIBLE: {
            R.FORECAST_EFFECT_BELOW_ONE_UNIT,
            R.PROMISE_UNREACHABLE_LEADTIME,
            R.NO_TRANSFERABLE_STOCK,
            R.NO_RECOVERY_EFFECT,
        },
    },
}

# 6.1절 표 B: 대응안 → 허용 미회복 사유
NOT_RECOVERED_REASONS: dict[OptionType, set[NotRecoveredReason]] = {
    O.KEEP_PLAN: {NotRecoveredReason.NO_RECOVERY_EFFECT},
    O.PRE_DEPARTURE_AIR: {
        NotRecoveredReason.PROMISE_UNREACHABLE_LEADTIME,
        NotRecoveredReason.NO_RECOVERY_EFFECT,
    },
    O.KR_AIR_REPLENISH: {
        NotRecoveredReason.PROMISE_UNREACHABLE_LEADTIME,
        NotRecoveredReason.NO_KR_ELIGIBLE_STOCK,
        NotRecoveredReason.NO_RECOVERY_EFFECT,
    },
    O.OTHER_REGION_FULFILL: {
        NotRecoveredReason.PROMISE_UNREACHABLE_LEADTIME,
        NotRecoveredReason.DONOR_NO_NEXT_INBOUND,
        NotRecoveredReason.NO_TRANSFERABLE_STOCK,
    },
}


class CalculateOptionOutcomesInput(ToolModel):
    """T10 입력: T9 출력과 T1·T2·T3·T4·T5·T8, T6·T7 출력"""

    option_feasibility: OptionFeasibility
    delay_context: DelayContext
    inventory_snapshot: InventorySnapshot
    inbound_schedule: InboundSchedule
    sales_history: SalesHistory
    open_orders: OpenOrders
    operating_rules: OperatingRules
    inventory_impact: InventoryImpact
    at_risk_orders: AtRiskOrders


class OrderLineOutcome(ToolModel):
    """위험 주문 상세 1개의 결과 (D21). 정렬: order_line_id. 서비스가 order_line_id 를 at_risk_order_id 로 바꿔 저장한다."""

    order_line_id: ToolId
    outcome: OrderOutcome
    not_recovered_reason: NotRecoveredReason | None
    line_securement_status: LineSecurementStatus
    secured_quantity: StrictInt
    recovered_quantity: StrictInt = Field(ge=0)  # option_order_outcomes_ck2
    expected_delivery_at: UtcTimestamp | None  # RECOVERED 일 때 배송일 T23:59:59Z
    source_warehouse_id: ToolId | None
    supply_source: SupplySource | None

    @model_validator(mode="after")
    def _match_ddl(self) -> Self:
        recovered = self.outcome == OrderOutcome.RECOVERED
        checks = [
            (
                recovered == (self.recovered_quantity > 0),
                "ck3: NOT_RECOVERED 이면 회복 수량 0, RECOVERED 이면 0보다 큼",
            ),
            (
                self.secured_quantity >= self.recovered_quantity,
                "ck4: 확보 수량 ≥ 회복 수량",
            ),
            (
                (self.secured_quantity > 0) == (self.source_warehouse_id is not None),
                "ck5: 확보 수량 > 0 일 때만 공급 창고",
            ),
            (
                (self.source_warehouse_id is None) == (self.supply_source is None),
                "ck6: 공급 창고와 공급 원천은 함께 있거나 함께 없음",
            ),
            (
                not recovered
                or self.line_securement_status == LineSecurementStatus.FULLY_SECURED,
                "ck9: RECOVERED 는 FULLY_SECURED",
            ),
            (
                (self.line_securement_status == LineSecurementStatus.NOT_SECURED)
                == (self.secured_quantity == 0),
                "ck10: NOT_SECURED ⇔ 확보 수량 0",
            ),
            (
                recovered == (self.not_recovered_reason is None),
                "ck11: RECOVERED 이면 미회복 사유 없음, NOT_RECOVERED 이면 있음",
            ),
            (
                recovered == (self.expected_delivery_at is not None),
                "배송 시각은 RECOVERED 일 때만 (계약 6절)",
            ),
        ]
        for ok, message in checks:
            if not ok:
                raise ValueError(f"option_order_outcomes {message}")
        return self


class OptionOutcome(ToolModel):
    """대응안 1개 = response_options 1행 + option_order_outcomes 여러 행. 식별자와 reason 열은 서비스가 채운다."""

    option_type: OptionType
    feasibility_status: FeasibilityStatus
    reason_code: OptionReasonCode | None
    required_quantity: StrictInt = Field(ge=0)  # response_options_ck11
    securable_quantity: StrictInt | None = Field(ge=0)
    reduction_committed_total: StrictInt | None = Field(ge=0)
    expected_delivery_date: ToolDate | None  # 공급일 S
    fully_recovered_orders: StrictInt | None
    delay_induced_recovered_orders: StrictInt | None
    item_secured_blocked_orders: StrictInt | None
    not_recovered_orders: StrictInt | None
    sku_results: SkuResults
    outcomes: list[OrderLineOutcome]

    @model_validator(mode="after")
    def _match_table_a_and_ddl(self) -> Self:
        allowed = OPTION_REASONS[self.option_type].get(self.feasibility_status)
        if allowed is None or self.reason_code not in allowed:
            raise ValueError(
                f"{self.option_type} {self.feasibility_status} / {self.reason_code} 은 6.1절 표 A에 없습니다"
            )
        aggregates = [
            self.fully_recovered_orders,
            self.delay_induced_recovered_orders,
            self.item_secured_blocked_orders,
            self.not_recovered_orders,
        ]
        no_result = self.fully_recovered_orders is None
        if any((a is None) != no_result for a in aggregates):
            raise ValueError(
                "주문 집계 4개는 함께 null 이거나 함께 값이 있습니다 (response_options_ck8)"
            )
        if (self.securable_quantity is None) != no_result or (
            self.reduction_committed_total is None
        ) != no_result:
            raise ValueError(
                "주문 결과를 만들지 않은 대응안만 확보 가능 수량·확정 감소 합이 null 입니다 (계약 6절)"
            )
        if no_result and self.outcomes:
            raise ValueError(
                "집계가 null 인 대응안은 주문 결과가 없습니다 (판정 순서 1·2)"
            )
        if self.feasibility_status == F.FEASIBLE and no_result:
            raise ValueError("FEASIBLE 은 집계가 있어야 합니다 (response_options_ck9)")
        if not no_result:
            fully, induced, blocked, not_rec = aggregates
            if not (
                fully >= 0
                and not_rec >= 0
                and 0 <= blocked <= not_rec
                and 0 <= induced <= fully
            ):
                raise ValueError(
                    "주문 집계 범위가 맞지 않습니다 (response_options_ck10)"
                )
        induced = self.delay_induced_recovered_orders or 0
        committed = self.reduction_committed_total or 0
        if (
            self.feasibility_status == F.FEASIBLE
            and self.option_type != O.KEEP_PLAN
            and not (induced >= 1 or committed >= 1)
        ):
            raise ValueError(
                "KEEP_PLAN 외 FEASIBLE 은 지연 유발 회복 ≥ 1 또는 확정 감소 ≥ 1 (response_options_ck12)"
            )
        if self.reason_code == R.FORECAST_EFFECT_BELOW_ONE_UNIT and not (
            self.reduction_committed_total == 0 and induced == 0
        ):
            raise ValueError(
                "FORECAST_EFFECT_BELOW_ONE_UNIT 은 확정 감소 0·지연 유발 회복 0 (response_options_ck13)"
            )
        if self.option_type == O.KEEP_PLAN and not (committed == 0 and induced == 0):
            raise ValueError(
                "KEEP_PLAN 은 확정 감소 0·지연 유발 회복 0 (response_options_ck14)"
            )
        for line in self.outcomes:
            if (
                line.not_recovered_reason is not None
                and line.not_recovered_reason
                not in NOT_RECOVERED_REASONS[self.option_type]
            ):
                raise ValueError(
                    f"{self.option_type} 의 미회복 사유 {line.not_recovered_reason} 은 6.1절 표 B에 없습니다"
                )
        return self


class OptionOutcomes(ToolModel):
    """T10 출력: T9 와 같은 순서"""

    options: list[OptionOutcome]

    @model_validator(mode="after")
    def _fixed_order(self) -> Self:
        _check_option_order([o.option_type for o in self.options])
        return self


# --- T11 compare_options -------------------------------------------------------------------------


class OptionComparisonInput(ToolModel):
    """T11 입력 항목: T10 options[] 에서 순위에 쓰는 값만"""

    option_type: OptionType
    feasibility_status: FeasibilityStatus
    delay_induced_recovered_orders: StrictInt | None
    reduction_committed_total: StrictInt | None
    expected_delivery_date: ToolDate | None


class CompareOptionsInput(ToolModel):
    options: list[OptionComparisonInput]

    @model_validator(mode="after")
    def _fixed_order(self) -> Self:
        _check_option_order([o.option_type for o in self.options])
        return self


class OptionRank(ToolModel):
    option_type: OptionType
    recommendation_rank: StrictInt | None = Field(
        ge=1
    )  # FEASIBLE 만 1부터, 그 밖은 null
    is_recommended: StrictBool  # 순위 1

    @model_validator(mode="after")
    def _recommended_is_rank_one(self) -> Self:
        if self.is_recommended != (self.recommendation_rank == 1):
            raise ValueError("추천은 순위 1 이어야 합니다 (response_options_ck16)")
        return self


class OptionRanking(ToolModel):
    """T11 출력: T10 과 같은 순서. 추천 1건·순위 중복 없음은 DB 유일 제약(is_recommended_uq·rank_uq)과 같다."""

    options: list[OptionRank]

    @model_validator(mode="after")
    def _ranks_are_unique_and_contiguous(self) -> Self:
        _check_option_order([o.option_type for o in self.options])
        ranks = sorted(
            o.recommendation_rank
            for o in self.options
            if o.recommendation_rank is not None
        )
        if ranks != list(range(1, len(ranks) + 1)):
            raise ValueError("순위는 1부터 빠짐·중복 없이 매깁니다")
        keep = next(o for o in self.options if o.option_type == O.KEEP_PLAN)
        if keep.recommendation_rank is not None and keep.recommendation_rank != len(
            ranks
        ):
            raise ValueError("KEEP_PLAN 은 순위의 맨 뒤입니다 (7절 순위 규칙)")
        return self
