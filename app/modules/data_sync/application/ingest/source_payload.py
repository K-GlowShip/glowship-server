from dataclasses import dataclass
from datetime import date, datetime

from app.modules.data_sync.application.ingest.source_records import (
    SkuRecord,
    WarehouseRecord,
)

"""
    원천 시스템이 보내는 레코드 (외부 코드 그대로, GLOW-13)
      - 가상 API(GLOW-49)·파일 수집은 원천 응답을 이 모양으로 바꿔 적재 서비스에 넘깁니다.
      - SKU·창고는 외부 코드(sku_code·warehouse_code)로 오고, 적재 서비스가 내부 ID 로 바꿉니다 (code_resolver).
        ERP(상품·주문)는 내부 코드와 같은 코드를 쓰고, WMS(재고)·TMS(선적)는 external_entity_mappings 로 바꿉니다.
      - 상품·창고 마스터는 ID 가 없으므로 비교용 레코드(SkuRecord·WarehouseRecord)를 그대로 씁니다.
      - 선적은 TMS 응답 그대로입니다(shipmentNo·mode·destWh·plannedKrRelease·stage·etaPort·eta3pl·actual3pl·items).
        미국 항구·시장 리드타임 기준·계획 3PL 입고일은 보내지 않고 서버가 정합니다 (code_resolver 참고).
        transport_mode 는 OCEAN·AIR 입니다.
"""

SourceSku = SkuRecord
SourceWarehouse = WarehouseRecord


@dataclass(frozen=True)
class SourceOrderLine:
    line_no: int
    sku_code: str
    ordered_quantity: int
    shipped_quantity: int
    line_status: str


@dataclass(frozen=True)
class SourceOrder:
    order_code: str
    sales_channel: str
    customer_region: str
    warehouse_code: str
    ordered_at: datetime
    promised_delivery_at: datetime
    status: str
    lines: tuple[SourceOrderLine, ...]


@dataclass(frozen=True)
class SourceShipmentLine:
    line_no: int
    sku_code: str
    shipped_quantity: int


@dataclass(frozen=True)
class SourceShipment:
    shipment_code: str
    transport_mode: str
    warehouse_code: str
    planned_kr_release_date: date
    current_stage: str
    current_expected_port_arrival_date: date | None
    current_expected_3pl_inbound_date: date
    actual_3pl_inbound_date: date | None
    lines: tuple[SourceShipmentLine, ...]


@dataclass(frozen=True)
class SourceInventory:
    inventory_date: date
    warehouse_code: str
    sku_code: str
    on_hand_quantity: int
    reserved_quantity: int
    available_quantity: int
    outbound_quantity: int


SourceRecord = (
    SourceSku | SourceWarehouse | SourceOrder | SourceShipment | SourceInventory
)
