import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import ClassVar

"""
    수집 적재 입력 레코드 (GLOW-13, serviceflow 5.2 "수집 적재")
      - 원천 시스템(ERP·WMS·TMS) 응답을 정규화하고 외부 코드를 내부 ID로 바꾼 뒤의 모양입니다.
        외부 코드 → 내부 ID 변환(external_entity_mappings)은 적재 서비스가 하고, 여기서는 비교만 합니다.
      - KEY 는 업무 키(같은 행을 찾는 기준), FIELDS 는 값이 같은지 비교하는 업무 필드입니다.
        FIELDS 에 없는 값(표시용 코드 등)은 바뀌어도 변경으로 보지 않습니다.
      - 원천 삭제는 행을 지우지 않고 상태 REMOVED_AT_SOURCE 로 보냅니다. 응답에서 빠진 행은 삭제로 보지 않습니다.
"""

REMOVED_AT_SOURCE = "REMOVED_AT_SOURCE"


@dataclass(frozen=True)
class SkuRecord:
    """상품 (원천 PRODUCT, ERP). 업무 키 sku_code"""

    KEY: ClassVar[tuple[str, ...]] = ("sku_code",)
    FIELDS: ClassVar[tuple[str, ...]] = (
        "product_name",
        "brand_name",
        "hs10",
        "category",
        "subcategory",
        "popularity_tier",
        "unit_code",
        "units_per_case",
        "emergency_air_eligible",
        "status",
    )

    sku_code: str
    product_name: str
    brand_name: str
    hs10: str | None
    category: str
    subcategory: str | None
    popularity_tier: str
    unit_code: str
    units_per_case: int
    emergency_air_eligible: bool
    status: str


@dataclass(frozen=True)
class WarehouseRecord:
    """창고 (원천 WAREHOUSE, WMS). 업무 키 warehouse_code"""

    KEY: ClassVar[tuple[str, ...]] = ("warehouse_code",)
    FIELDS: ClassVar[tuple[str, ...]] = (
        "warehouse_name",
        "warehouse_type",
        "region_code",
    )

    warehouse_code: str
    warehouse_name: str
    warehouse_type: str
    region_code: str


@dataclass(frozen=True)
class OrderLineRecord:
    """주문 상세. 업무 키는 주문 안의 line_no"""

    KEY: ClassVar[tuple[str, ...]] = ("line_no",)
    FIELDS: ClassVar[tuple[str, ...]] = (
        "sku_id",
        "ordered_quantity",
        "shipped_quantity",
        "line_status",
    )

    line_no: int
    sku_id: uuid.UUID
    ordered_quantity: int
    shipped_quantity: int
    line_status: str


@dataclass(frozen=True)
class OrderRecord:
    """주문 (원천 ORDER, ERP). 업무 키 order_code. 상세는 lines 로 함께 옵니다"""

    KEY: ClassVar[tuple[str, ...]] = ("order_code",)
    FIELDS: ClassVar[tuple[str, ...]] = (
        "sales_channel",
        "customer_region",
        "warehouse_id",
        "ordered_at",
        "promised_delivery_at",
        "status",
    )

    order_code: str
    sales_channel: str
    customer_region: str
    warehouse_id: uuid.UUID
    ordered_at: datetime
    promised_delivery_at: datetime
    status: str
    lines: tuple[OrderLineRecord, ...]


@dataclass(frozen=True)
class ShipmentLineRecord:
    """선적 상세. 업무 키는 선적 안의 line_no"""

    KEY: ClassVar[tuple[str, ...]] = ("line_no",)
    FIELDS: ClassVar[tuple[str, ...]] = ("sku_id", "shipped_quantity")

    line_no: int
    sku_id: uuid.UUID
    shipped_quantity: int


# 일정 스냅샷(shipment_schedule_snapshots)을 새로 남기는 선적 필드. 지연 감지가 이 스냅샷을 비교합니다.
SCHEDULE_FIELDS = (
    "current_stage",
    "current_expected_port_arrival_date",
    "current_expected_3pl_inbound_date",
)


@dataclass(frozen=True)
class ShipmentRecord:
    """선적 (원천 SHIPMENT, TMS). 업무 키 shipment_code. 상세는 lines 로 함께 옵니다"""

    KEY: ClassVar[tuple[str, ...]] = ("shipment_code",)
    FIELDS: ClassVar[tuple[str, ...]] = (
        "transport_mode",
        "warehouse_id",
        "market_leadtime_reference_id",
        "planned_kr_release_date",
        "planned_3pl_inbound_date",
        *SCHEDULE_FIELDS,
        "actual_3pl_inbound_date",
    )

    shipment_code: str
    transport_mode: str
    warehouse_id: uuid.UUID
    market_leadtime_reference_id: uuid.UUID
    planned_kr_release_date: date
    planned_3pl_inbound_date: date
    current_stage: str
    current_expected_port_arrival_date: date | None
    current_expected_3pl_inbound_date: date
    actual_3pl_inbound_date: date | None
    lines: tuple[ShipmentLineRecord, ...]


@dataclass(frozen=True)
class InventoryRecord:
    """일별 재고 (원천 INVENTORY, WMS). 업무 키 (inventory_date, warehouse_id, sku_id)"""

    KEY: ClassVar[tuple[str, ...]] = ("inventory_date", "warehouse_id", "sku_id")
    FIELDS: ClassVar[tuple[str, ...]] = (
        "on_hand_quantity",
        "reserved_quantity",
        "available_quantity",
        "outbound_quantity",
    )

    inventory_date: date
    warehouse_id: uuid.UUID
    sku_id: uuid.UUID
    on_hand_quantity: int
    reserved_quantity: int
    available_quantity: int
    outbound_quantity: int
    # 변경 요약 문장용 표시 코드 (비교하지 않음)
    warehouse_code: str
    sku_code: str
