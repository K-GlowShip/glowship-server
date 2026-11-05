from typing import Any, Literal, Self

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from app.modules.delay_analysis.application.agent.tools.common import (
    RawQuantity,
    SignedRawQuantity,
    ToolDate,
    ToolId,
    ToolModel,
    UniqueCodes,
    UtcTimestamp,
    end_of_day,
)
from app.modules.delay_analysis.application.agent.tools.lookup_schemas import (
    DelayContext,
    InboundSchedule,
    InventorySnapshot,
    OpenOrders,
    OperatingRules,
    SalesHistory,
)
from app.modules.delay_analysis.infrastructure.analysis_enums import (
    CompletedStatus,
    ResultClass,
    RiskAttribution,
    SkuScope,
    WaitCause,
)
from app.modules.order.infrastructure.order_enums import SalesChannel

"""
    재고 영향 계산 도구 T7·T6 (docs/agent-flow/tool-contracts.md 4절, C 구현)
      - T7(위험 주문)을 먼저 실행하고, T6(재고 영향)이 T7의 delay_induced_sku_codes 를 받는다(9절 Q3).
      - 출력은 T9~T11이 읽는 값만 담는다. 저장 열 가운데 여기에 없는 것(T6 available_quantity·committed_open_quantity·
        average_daily_sales·calculation_status·warehouse_id, T7 risk_reason)은 저장 단계에서 더한다(9절 Q11).
      - 검증 규칙은 DDL CHECK 와 같다. DB가 거부할 값을 도구 경계에서 먼저 막기 위해서다.
"""


# --- T7 identify_at_risk_orders ------------------------------------------------------------------


class IdentifyAtRiskOrdersInput(ToolModel):
    """T7 입력: T1·T2·T3·T5·T8 출력 (재고 영향은 쓰지 않는다)"""

    delay_context: DelayContext
    inventory_snapshot: InventorySnapshot
    inbound_schedule: InboundSchedule
    open_orders: OpenOrders
    operating_rules: OperatingRules


class AtRiskOrderLine(ToolModel):
    """활성 상세. 정렬: line_no"""

    order_line_id: ToolId
    line_no: StrictInt
    sku_code: StrictStr
    open_quantity: StrictInt = Field(gt=0)  # at_risk_orders_ck1
    wait_cause: WaitCause | None


class AtRiskOrder(ToolModel):
    """위험 주문(주문 단위). 정렬: order_code"""

    order_id: ToolId
    order_code: StrictStr
    sales_channel: SalesChannel
    ordered_at: UtcTimestamp
    promised_delivery_at: UtcTimestamp
    risk_attribution: RiskAttribution
    expected_delivery_date: ToolDate  # E 시뮬레이션의 배송일
    # P 시뮬레이션의 배송일. DDL 은 DELAY_INDUCED 일 때 NULL 을 허용하지만(at_risk_orders_ck6), 엔진은 늘 값을 낸다:
    # 계획일 기준으로 확보되지 않으면 구간 끝 + 출고·배송 리드타임으로 정하고(core.order_layer), storage.py 가 늘 저장한다
    baseline_expected_delivery_date: ToolDate
    expected_beyond_horizon: StrictBool
    lines: list[AtRiskOrderLine] = Field(min_length=1)

    @model_validator(mode="after")
    def _match_ddl(self) -> Self:
        # 저장 시각은 배송일 T23:59:59Z (엔진 storage.py). 시각 문자열은 같은 모양이라 문자열 비교가 시각 비교와 같다.
        expected = end_of_day(self.expected_delivery_date)
        baseline = end_of_day(self.baseline_expected_delivery_date)
        if not expected > self.promised_delivery_at:
            raise ValueError(
                "위험 주문은 예상 배송이 약속보다 늦어야 합니다 (at_risk_orders_ck2)"
            )
        late_at_baseline = baseline > self.promised_delivery_at
        if (
            self.risk_attribution == RiskAttribution.PRE_EXISTING
            and not late_at_baseline
        ):
            raise ValueError(
                "PRE_EXISTING 은 계획일 기준 배송도 약속보다 늦어야 합니다 (at_risk_orders_ck5)"
            )
        if self.risk_attribution == RiskAttribution.DELAY_INDUCED and late_at_baseline:
            raise ValueError(
                "DELAY_INDUCED 는 계획일 기준 배송이 약속 안이어야 합니다 (at_risk_orders_ck6)"
            )
        return self


class AtRiskOrders(ToolModel):
    """T7 출력"""

    risks: list[AtRiskOrder]
    delay_induced_sku_codes: (
        UniqueCodes  # DELAY_INDUCED 주문의 모든 활성 상세 SKU(K_SHIPMENT 밖 포함)
    )
    simulation_horizon_end: ToolDate  # max(E, 약속일들) + 14일
    # E 시뮬레이션 결과(주문별 확보일, 일별 보호량). 형식은 엔진 연결 작업에서 정한다(9절 Q1·Q12).
    expected_plan_simulation: dict[str, Any]

    @model_validator(mode="after")
    def _delay_induced_skus_cover_orders(self) -> Self:
        induced = {
            line.sku_code
            for risk in self.risks
            if risk.risk_attribution == RiskAttribution.DELAY_INDUCED
            for line in risk.lines
        }
        if induced != set(self.delay_induced_sku_codes):
            raise ValueError(
                "delay_induced_sku_codes 는 DELAY_INDUCED 주문 활성 상세의 SKU 와 같아야 합니다"
            )
        return self


# --- T6 calculate_inventory_impact ---------------------------------------------------------------


class CalculateInventoryImpactInput(ToolModel):
    """T6 입력: T1·T2·T3·T4·T5·T8 출력과 T7 의 지연 유발 SKU"""

    delay_context: DelayContext
    inventory_snapshot: InventorySnapshot
    inbound_schedule: InboundSchedule
    sales_history: SalesHistory
    open_orders: OpenOrders
    operating_rules: OperatingRules
    delay_induced_sku_codes: UniqueCodes


class DailyDeficit(ToolModel):
    """D ≤ date < E 의 일별 부족(raw). 한국 창고 항공이 공급일 이후 가장 큰 값만큼 보낸다."""

    date: ToolDate
    deficit: RawQuantity


class ForecastPath(ToolModel):
    """기준선(P 입고) 또는 전체(E 입고) 한쪽의 예측 결과"""

    stockout_date: ToolDate | None  # 품절이 없으면 null
    stockout_days: StrictInt = Field(ge=0)
    safety_stock_breach_date: ToolDate | None  # 안전재고 하회가 없으면 null
    min_gap: (
        SignedRawQuantity | None
    )  # 가용 − 누적 수요의 최솟값. 계산할 날이 없으면(D ≥ 입고일) null (9절 Q10)
    daily_deficits: list[DailyDeficit]


class InventoryImpactItem(ToolModel):
    """SKU별 예측 층: K_SHIPMENT SKU + K_SHIPMENT 밖 지연 유발 SKU(지연 수량 0, IMPACTED). 정렬: sku_code"""

    sku_id: ToolId
    sku_code: StrictStr
    sku_scope: SkuScope
    raw_baseline_shortage: RawQuantity
    raw_total_shortage: RawQuantity
    raw_increment_shortage: RawQuantity
    baseline_prepare_quantity: StrictInt = Field(ge=0)
    increment_prepare_quantity: StrictInt = Field(ge=0)
    total_prepare_quantity: StrictInt = Field(ge=0)
    safety_stock_quantity: StrictInt = Field(ge=0)
    baseline: ForecastPath
    total: ForecastPath

    @model_validator(mode="after")
    def _total_is_sum(self) -> Self:
        if (
            self.total_prepare_quantity
            != self.baseline_prepare_quantity + self.increment_prepare_quantity
        ):
            raise ValueError(
                "total_prepare_quantity = baseline + increment 이어야 합니다"
            )
        return self


class InventoryImpact(ToolModel):
    """T6 출력"""

    items: list[InventoryImpactItem]
    impacted_sku_codes: (
        UniqueCodes  # K_IMPACTED (지연 유발 주문의 K_SHIPMENT 밖 SKU 포함)
    )
    watch_sku_codes: UniqueCodes  # K_WATCH
    completed_status: Literal[CompletedStatus.READY_FOR_REVIEW, CompletedStatus.NO_RISK]
    result_class: ResultClass

    @model_validator(mode="after")
    def _match_sets_and_ddl(self) -> Self:
        impacted, watch = set(self.impacted_sku_codes), set(self.watch_sku_codes)
        if impacted & watch:
            raise ValueError("한 SKU가 K_IMPACTED 와 K_WATCH 에 함께 있을 수 없습니다")
        for item in self.items:
            expected = (
                SkuScope.IMPACTED
                if item.sku_code in impacted
                else SkuScope.WATCH
                if item.sku_code in watch
                else SkuScope.SHIPMENT_ONLY
            )
            if item.sku_scope != expected:
                raise ValueError(
                    f"{item.sku_code} 의 sku_scope 는 영향 집합과 맞게 {expected} 이어야 합니다"
                )
        allowed = {
            CompletedStatus.READY_FOR_REVIEW: {
                ResultClass.READY_ORDER_RISK,
                ResultClass.FORECAST_ONLY_READY,
            },
            CompletedStatus.NO_RISK: {
                ResultClass.PURE_NO_RISK,
                ResultClass.WARNING_NO_RISK,
            },
        }
        if self.result_class not in allowed[self.completed_status]:
            raise ValueError(
                "결과 분류가 완료 판정과 맞지 않습니다 (analysis_runs_ck21)"
            )
        return self
