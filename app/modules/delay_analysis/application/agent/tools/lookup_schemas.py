from typing import Any, Literal

from pydantic import Field, StrictBool, StrictInt, StrictStr

from app.modules.delay_analysis.application.agent.tools.common import (
    RegionCode,
    Sha256Hex,
    ShipmentStage,
    ToolDate,
    ToolId,
    ToolModel,
    TransportMode,
    UtcTimestamp,
)
from app.modules.delay_analysis.infrastructure.analysis_enums import SalesBasisType
from app.modules.order.infrastructure.order_enums import OrderLineStatus, SalesChannel

"""
    조회 도구 T1~T5·T8 출력 = 분석 입력 스냅샷 (docs/agent-flow/tool-contracts.md 3절)
      - 모든 조회는 실행의 입력 순번(input_seq) 이하로 적용된 수집이 적재한 값만 읽는다(1절 "입력 기준").
      - 배열 정렬 키는 각 클래스 설명에 적었다(문서 표의 "배열(`키`)"). 정렬은 도구가 맞추고, 모델은 검사하지 않는다.
      - T5 allocation_state 의 필드 형식은 엔진 연결 작업에서 정한다(9절 Q1·Q12). 지금은 자리만 둔다.
"""


# --- T1 get_delay_context ------------------------------------------------------------------------


class GetDelayContextInput(ToolModel):
    """T1 입력. D와 입력 순번은 실행 생성(GLOW-99)이 정한다."""

    analysis_run_id: ToolId
    delay_event_id: ToolId
    base_date: ToolDate
    input_seq: StrictInt = Field(ge=0)


class WarehouseRef(ToolModel):
    """창고 요약 (3.1절 표 아래)"""

    warehouse_id: ToolId
    warehouse_code: StrictStr
    region_code: RegionCode


class ShipmentSku(ToolModel):
    """지연 선적의 SKU = K_SHIPMENT. 정렬: sku_code"""

    sku_id: ToolId
    sku_code: StrictStr
    delayed_quantity: StrictInt = Field(ge=0)


class DelayContext(ToolModel):
    """T1 출력"""

    shipment_id: ToolId
    base_date: ToolDate
    planned_3pl_inbound_date: ToolDate
    expected_3pl_inbound_date: ToolDate
    stage: ShipmentStage
    transport_mode: TransportMode
    planned_kr_release_date: ToolDate
    trigger_shipment_schedule_snapshot_id: ToolId
    shipment_skus: list[ShipmentSku]
    destination_warehouse: WarehouseRef
    donor_warehouse: WarehouseRef
    kr_origin_warehouse: WarehouseRef


# --- T2 get_inventory_snapshot -------------------------------------------------------------------


class InventoryItem(ToolModel):
    """정렬: sku_code"""

    sku_code: StrictStr
    on_hand_quantity: StrictInt
    reserved_quantity: StrictInt


class WarehouseInventory(ToolModel):
    """정렬: warehouse_code. 도착·공여 창고 2개"""

    warehouse_code: StrictStr
    items: list[InventoryItem]


class KrOnHand(ToolModel):
    """한국 창고 보유 (행이 있는 SKU만). 정렬: sku_code"""

    sku_code: StrictStr
    on_hand_quantity: StrictInt


class InventorySnapshot(ToolModel):
    """T2 출력. D−1 마감 재고. 행 누락·음수 가용 재고 확인은 입력 검증(GLOW-100)이 한다."""

    inventory_date: ToolDate
    warehouses: list[WarehouseInventory]
    kr_on_hand: list[KrOnHand]


# --- T3 get_inbound_schedule ---------------------------------------------------------------------


class SkuQuantity(ToolModel):
    """정렬: sku_code"""

    sku_code: StrictStr
    quantity: StrictInt = Field(ge=0)


class InboundShipment(ToolModel):
    """입고 예정 선적. 정렬: (warehouse_code, arrival_date, shipment_id)"""

    shipment_id: ToolId
    warehouse_id: ToolId
    warehouse_code: StrictStr
    expected_3pl_inbound_date: ToolDate
    arrival_date: ToolDate  # max(예상일, D)
    quantities: list[SkuQuantity]


class DonorNextInbound(ToolModel):
    """정렬: sku_code. 공여 창고에 그 SKU가 들어오는 가장 이른 예상일(≥ D). 없으면 null (R-03)"""

    sku_code: StrictStr
    next_inbound_date: ToolDate | None


class InboundSchedule(ToolModel):
    """T3 출력"""

    inbound: list[InboundShipment]
    donor_next_inbound: list[DonorNextInbound]


# --- T4 get_sales_history ------------------------------------------------------------------------


class DemandTotal(ToolModel):
    """28일 유효 주문 수요 합. 정렬: (warehouse_code, sku_code, sales_channel). 일평균 = quantity / basis_days"""

    warehouse_code: StrictStr
    sku_code: StrictStr
    sales_channel: SalesChannel
    quantity: StrictInt = Field(ge=0)


class ActualDemand(ToolModel):
    """STALE 재계산 때만. 입력 순번 뒤에 적용된 수집의 그날 실제 수요. 정렬: (warehouse_code, sku_code, demand_date)"""

    warehouse_code: StrictStr
    sku_code: StrictStr
    demand_date: ToolDate
    quantity: StrictInt = Field(ge=0)


class SalesHistory(ToolModel):
    """T4 출력. 일반 실행에서는 actual_* 두 배열이 비어 있다."""

    sales_basis_type: Literal[SalesBasisType.ORDER_DEMAND]
    basis_start_date: ToolDate
    basis_end_date: ToolDate
    basis_days: Literal[28]
    demand_totals: list[DemandTotal]
    event_days: StrictInt = Field(ge=0)
    event_names: list[StrictStr]
    calendar_version: StrictStr
    calendar_sha256: Sha256Hex
    actual_known_days: list[ToolDate]
    actual_demand: list[ActualDemand]


# --- T5 get_open_orders --------------------------------------------------------------------------


class OpenOrderLine(ToolModel):
    """정렬: line_no. 배정에는 ACTIVE 만 쓴다."""

    order_line_id: ToolId
    line_no: StrictInt
    sku_id: ToolId
    sku_code: StrictStr
    ordered_quantity: StrictInt
    line_status: OrderLineStatus


class OpenOrder(ToolModel):
    """D−1 마감에 열린 주문. 정렬: order_code"""

    order_id: ToolId
    order_code: StrictStr
    warehouse_id: ToolId
    sales_channel: SalesChannel
    ordered_at: UtcTimestamp
    promised_delivery_at: UtcTimestamp
    lines: list[OpenOrderLine]


class OpenOrders(ToolModel):
    """T5 출력"""

    orders: list[OpenOrder]
    # D−1 배정 하루 재생 결과(창고별). 형식은 엔진 연결 작업에서 정한다(9절 Q1·Q12).
    allocation_state: list[dict[str, Any]]


# --- T8 get_operating_rules ----------------------------------------------------------------------


class LeadTimes(ToolModel):
    """리드타임(일). null = 운영 기준에 그 행이 없음 또는 day_basis = BUSINESS_DAY (9절 Q5)"""

    dest_outbound_handling_days: StrictInt | None
    dest_customer_delivery_days: StrictInt | None
    donor_outbound_handling_days: StrictInt | None
    donor_customer_delivery_days: StrictInt | None
    kr_outbound_handling_days: StrictInt | None
    kr_air_transit_days: StrictInt | None
    dest_inbound_handling_days: StrictInt | None


class SkuAttribute(ToolModel):
    """정렬: sku_code"""

    sku_id: ToolId
    sku_code: StrictStr
    emergency_air_eligible: StrictBool  # 9절 Q8


class OperatingRules(ToolModel):
    """T8 출력. D 시점 유효한 최신 버전(effective_from ≤ D)"""

    operating_rule_set_id: ToolId
    version_no: StrictInt
    safety_stock_days: StrictInt
    # 계산은 28 고정. 28이 아니면 입력 검증이 ON_HOLD(REQUIRED_DATA_INVALID)로 막는다(9절 Q6). 모델은 값 그대로 받는다.
    sales_basis_days: StrictInt
    lead_times: LeadTimes
    skus: list[SkuAttribute]
