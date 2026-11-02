from enum import StrEnum

"""
    분석 실행·결과 코드값 (ERD v6.2 CHECK 제약과 같은 값)
      - 화면 표시명은 명세 2절(라벨 사전 labels/ko-v1)을 따른다. 여기에는 코드값만 둔다.
"""


class AnalysisRunStatus(StrEnum):
    """analysis_runs.status (analysis_runs_ck9)"""

    DETECTED = "DETECTED"
    VALIDATING = "VALIDATING"
    ANALYZING_IMPACT = "ANALYZING_IMPACT"
    ANALYZING_OPTIONS = "ANALYZING_OPTIONS"
    VALIDATING_OUTPUT = "VALIDATING_OUTPUT"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    NO_DELAY = "NO_DELAY"
    NO_RISK = "NO_RISK"
    ON_HOLD = "ON_HOLD"
    FAILED = "FAILED"
    STALE = "STALE"
    FINALIZED = "FINALIZED"


class CompletedStatus(StrEnum):
    """analysis_runs.completed_status: 완료 판정 당시 상태 (analysis_runs_ck19)"""

    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    NO_RISK = "NO_RISK"
    NO_DELAY = "NO_DELAY"
    ON_HOLD = "ON_HOLD"
    FAILED = "FAILED"


class ResultClass(StrEnum):
    """analysis_runs.result_class (analysis_runs_ck20)"""

    READY_ORDER_RISK = "READY_ORDER_RISK"  # 실주문 납기 위험
    FORECAST_ONLY_READY = "FORECAST_ONLY_READY"  # 예측 기준 부족 위험
    PURE_NO_RISK = "PURE_NO_RISK"  # 지연 영향 없음
    WARNING_NO_RISK = "WARNING_NO_RISK"  # 지연 영향 없음(기존 부족 있음)


class TriggerType(StrEnum):
    """analysis_runs.trigger_type (analysis_runs_ck1)"""

    AUTO = "AUTO"
    REANALYSIS = "REANALYSIS"


class TriggerReason(StrEnum):
    """analysis_runs.trigger_reason (analysis_runs_ck15, 명세 3절 실행 생성 경로)"""

    EPISODE_STARTED = "EPISODE_STARTED"
    SCHEDULE_WORSENED = "SCHEDULE_WORSENED"
    SCHEDULE_IMPROVED = "SCHEDULE_IMPROVED"
    STAGE_CHANGED = "STAGE_CHANGED"
    DELAY_RECOVERED = "DELAY_RECOVERED"
    USER_REANALYSIS = "USER_REANALYSIS"


class StaleReason(StrEnum):
    """analysis_runs.stale_primary_reason: STALE 대표 사유 (analysis_runs_ck28)
    사유가 2개 이상이면 MULTIPLE_RELEVANT_CHANGES 다(ck31)."""

    DELAY_SCHEDULE_CHANGED = "DELAY_SCHEDULE_CHANGED"
    DELAY_STAGE_CHANGED = "DELAY_STAGE_CHANGED"
    RELEVANT_ORDER_CHANGED = "RELEVANT_ORDER_CHANGED"
    RELEVANT_INVENTORY_CHANGED = "RELEVANT_INVENTORY_CHANGED"
    RELEVANT_INBOUND_CHANGED = "RELEVANT_INBOUND_CHANGED"
    OPERATING_RULE_CHANGED = "OPERATING_RULE_CHANGED"
    RELEVANT_SKU_ATTRIBUTE_CHANGED = "RELEVANT_SKU_ATTRIBUTE_CHANGED"
    TIME_ELAPSED = "TIME_ELAPSED"
    MULTIPLE_RELEVANT_CHANGES = "MULTIPLE_RELEVANT_CHANGES"


class StaleReasonCode(StrEnum):
    """analysis_runs.stale_reason_codes 배열의 항목 (analysis_runs_ck30)
    StaleReason 에서 MULTIPLE_RELEVANT_CHANGES 만 뺀 값이다. 배열에는 개별 사유만 들어간다."""

    DELAY_SCHEDULE_CHANGED = "DELAY_SCHEDULE_CHANGED"
    DELAY_STAGE_CHANGED = "DELAY_STAGE_CHANGED"
    RELEVANT_ORDER_CHANGED = "RELEVANT_ORDER_CHANGED"
    RELEVANT_INVENTORY_CHANGED = "RELEVANT_INVENTORY_CHANGED"
    RELEVANT_INBOUND_CHANGED = "RELEVANT_INBOUND_CHANGED"
    OPERATING_RULE_CHANGED = "OPERATING_RULE_CHANGED"
    RELEVANT_SKU_ATTRIBUTE_CHANGED = "RELEVANT_SKU_ATTRIBUTE_CHANGED"
    TIME_ELAPSED = "TIME_ELAPSED"


class FailedStep(StrEnum):
    """analysis_runs.failed_step (analysis_runs_ck12)"""

    VALIDATING = "VALIDATING"
    ANALYZING_IMPACT = "ANALYZING_IMPACT"
    ANALYZING_OPTIONS = "ANALYZING_OPTIONS"
    VALIDATING_OUTPUT = "VALIDATING_OUTPUT"


class ExplanationValidationStatus(StrEnum):
    """analysis_runs.explanation_validation_status (analysis_runs_ck5)"""

    PASSED = "PASSED"
    FAILED = "FAILED"


class SkuScope(StrEnum):
    """inventory_impact_results.sku_scope (inventory_impact_results_ck3)"""

    IMPACTED = "IMPACTED"  # 지연 증분이 있거나 지연 유발 위험 주문에 들어간 SKU
    WATCH = "WATCH"  # 증분은 없지만 안전재고 아래로 내려가는 SKU
    SHIPMENT_ONLY = "SHIPMENT_ONLY"  # 지연 선적에만 있는 나머지 SKU


class CalculationStatus(StrEnum):
    """inventory_impact_results.calculation_status (inventory_impact_results_ck1)"""

    CALCULATED = "CALCULATED"
    NOT_ESTIMABLE = "NOT_ESTIMABLE"


class SalesBasisType(StrEnum):
    """inventory_impact_results.sales_basis_type (inventory_impact_results_ck5)"""

    ORDER_DEMAND = "ORDER_DEMAND"
    OUTBOUND = "OUTBOUND"


class RiskAttribution(StrEnum):
    """at_risk_orders.risk_attribution (at_risk_orders_ck3)"""

    PRE_EXISTING = "PRE_EXISTING"  # 기존 위험
    DELAY_INDUCED = "DELAY_INDUCED"  # 지연 유발


class WaitCause(StrEnum):
    """at_risk_orders.wait_cause (at_risk_orders_ck4)"""

    DIRECT_SKU_SHORTAGE = "DIRECT_SKU_SHORTAGE"
    BUNDLED_ORDER_WAIT = "BUNDLED_ORDER_WAIT"


class OptionType(StrEnum):
    """response_options.option_type (response_options_ck1)"""

    KEEP_PLAN = "KEEP_PLAN"
    PRE_DEPARTURE_AIR = "PRE_DEPARTURE_AIR"
    KR_AIR_REPLENISH = "KR_AIR_REPLENISH"
    OTHER_REGION_FULFILL = "OTHER_REGION_FULFILL"


class FeasibilityStatus(StrEnum):
    """response_options.feasibility_status (response_options_ck2)"""

    FEASIBLE = "FEASIBLE"
    NOT_FEASIBLE = "NOT_FEASIBLE"
    NEEDS_CHECK = "NEEDS_CHECK"


class OptionReasonCode(StrEnum):
    """response_options.reason_code (response_options_ck6·ck7). FEASIBLE 이면 비어 있다."""

    ARRIVES_AFTER_DELAYED_SHIPMENT = "ARRIVES_AFTER_DELAYED_SHIPMENT"
    PROMISE_UNREACHABLE_LEADTIME = "PROMISE_UNREACHABLE_LEADTIME"
    NO_KR_ELIGIBLE_STOCK = "NO_KR_ELIGIBLE_STOCK"
    NO_TRANSFERABLE_STOCK = "NO_TRANSFERABLE_STOCK"
    FORECAST_EFFECT_BELOW_ONE_UNIT = "FORECAST_EFFECT_BELOW_ONE_UNIT"
    NO_RECOVERY_EFFECT = "NO_RECOVERY_EFFECT"
    DONOR_NO_NEXT_INBOUND = "DONOR_NO_NEXT_INBOUND"
    MISSING_OPERATING_RULE = "MISSING_OPERATING_RULE"
    MISSING_REQUIRED_DATA = "MISSING_REQUIRED_DATA"


class OrderOutcome(StrEnum):
    """option_order_outcomes.outcome (option_order_outcomes_ck1). 부분 회복은 없다."""

    RECOVERED = "RECOVERED"
    NOT_RECOVERED = "NOT_RECOVERED"


class NotRecoveredReason(StrEnum):
    """option_order_outcomes.not_recovered_reason (option_order_outcomes_ck12)"""

    PROMISE_UNREACHABLE_LEADTIME = "PROMISE_UNREACHABLE_LEADTIME"
    NO_KR_ELIGIBLE_STOCK = "NO_KR_ELIGIBLE_STOCK"
    NO_TRANSFERABLE_STOCK = "NO_TRANSFERABLE_STOCK"
    DONOR_NO_NEXT_INBOUND = "DONOR_NO_NEXT_INBOUND"
    NO_RECOVERY_EFFECT = "NO_RECOVERY_EFFECT"


class LineSecurementStatus(StrEnum):
    """option_order_outcomes.line_securement_status (option_order_outcomes_ck8)"""

    FULLY_SECURED = "FULLY_SECURED"
    PARTIALLY_SECURED = "PARTIALLY_SECURED"
    NOT_SECURED = "NOT_SECURED"


class SupplySource(StrEnum):
    """option_order_outcomes.supply_source (option_order_outcomes_ck7)"""

    EXISTING_STOCK = "EXISTING_STOCK"
    OTHER_REGION_WAREHOUSE = "OTHER_REGION_WAREHOUSE"
    KR_AIR = "KR_AIR"
    PRE_DEPARTURE_AIR = "PRE_DEPARTURE_AIR"


class ExplanationWarningCode(StrEnum):
    """explanation.v1 warnings[].code (설명 스키마 enum, 라벨 사전 warning)"""

    FORECAST_EVENT_OVERLAP = "FORECAST_EVENT_OVERLAP"
    BASELINE_SHORTAGE_EXISTS = "BASELINE_SHORTAGE_EXISTS"
    OPTION_NEEDS_CHECK = "OPTION_NEEDS_CHECK"
    SKU_NOT_ESTIMABLE = "SKU_NOT_ESTIMABLE"


class SkuEvaluationStatus(StrEnum):
    """sku_results.v1 items[].evaluation_status (JSON 계약 v3 1절)"""

    EVALUATED = "EVALUATED"
    NOT_ESTIMABLE = "NOT_ESTIMABLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"  # 이 대응안이 그 SKU에 공급하지 않음


class SkuNotEstimableReason(StrEnum):
    """sku_results.v1 items[].not_estimable_reason (JSON 계약 v3 1절)"""

    DONOR_NO_NEXT_INBOUND = "DONOR_NO_NEXT_INBOUND"
    MISSING_OPERATING_RULE = "MISSING_OPERATING_RULE"
    MISSING_REQUIRED_DATA = "MISSING_REQUIRED_DATA"


class ImpactSource(StrEnum):
    """sku_results.v1 items[].impact_source (JSON 계약 v3 0.1절). IMPACTED 일 때만 값이 있다."""

    FORECAST_INCREMENT = "FORECAST_INCREMENT"
    DELAY_INDUCED_ORDER_LINE = "DELAY_INDUCED_ORDER_LINE"
