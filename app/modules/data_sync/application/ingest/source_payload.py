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

    파서가 보장할 조건 (원천 응답 → 이 레코드. 적재 서비스는 아래를 다시 검사하지 않습니다)
      1. 타입: 필드에 선언한 타입 그대로 넘깁니다. 날짜는 date, 일시는 datetime, 수량은 int, 참거짓은 bool 이고
         문자열로 남기지 않습니다.
      2. 필수값: `| None` 이 아닌 필드는 None 이 아닙니다. 값이 없으면 그 레코드를 MISSING_REQUIRED 로 거부합니다.
      3. 일시는 시간대가 있는 UTC datetime 입니다. 시간대가 없거나 날짜·일시로 읽을 수 없는 값은 INVALID_DATE 로 거부합니다
         (시간대 없는 값이 들어오면 비교에서 TypeError 가 나 수집 전체가 실패합니다).
      4. 거부는 레코드 단위입니다. 상세 줄 하나가 원인이어도 부모 레코드 1건을 거부하고, 키는 부모 코드·위치는
         field_name(lines[i].필드)에 둡니다 (code_resolver 거부와 같은 형식). 오류 코드 이름은 확정 기준선
         data_validation_issues.csv 와 같습니다.
    값의 의미(음수·수량 관계·상태 값 등 DB 제약)는 파서가 아니라 적재 서비스가 적용 전에 검사합니다.
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
