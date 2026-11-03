import re
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import CheckConstraint

from app.modules.data_sync.application.ingest.record_validation import (
    CUSTOMER_REGIONS,
    INCONSISTENT_VALUE,
    INVALID_QUANTITY,
    INVALID_VALUE,
    LINE_STATUSES,
    NEGATIVE_QUANTITY,
    ORDER_STATUSES,
    POPULARITY_TIERS,
    REGION_CODES,
    SALES_CHANNELS,
    SHIPMENT_STAGES,
    SKU_STATUSES,
    WAREHOUSE_TYPES,
    validate_inventory,
    validate_order,
    validate_shipment,
    validate_sku,
    validate_warehouse,
)
from app.modules.data_sync.application.ingest.source_payload import (
    SourceInventory,
    SourceOrder,
    SourceOrderLine,
    SourceShipment,
    SourceShipmentLine,
    SourceSku,
    SourceWarehouse,
)
from app.modules.master_data.infrastructure.sku import Sku
from app.modules.master_data.infrastructure.warehouse import Warehouse
from app.modules.order.infrastructure.order import Order
from app.modules.order.infrastructure.order_line import OrderLine
from app.modules.shipment.infrastructure.shipment import Shipment

SKU = SourceSku(
    sku_code="SKU-001",
    product_name="세라마이드 크림",
    brand_name="GLOW",
    hs10="3304995000",
    category="SKINCARE",
    subcategory="CREAM",
    popularity_tier="HIT",
    unit_code="EA",
    units_per_case=24,
    emergency_air_eligible=True,
    status="ACTIVE",
)
WAREHOUSE = SourceWarehouse(
    warehouse_code="WH-WEST",
    warehouse_name="West 3PL",
    warehouse_type="US_3PL",
    region_code="WEST",
)
ORDER = SourceOrder(
    order_code="ORD-1",
    sales_channel="AMAZON",
    customer_region="WEST",
    warehouse_code="WH-WEST",
    ordered_at=datetime(2026, 1, 5, tzinfo=UTC),
    promised_delivery_at=datetime(2026, 1, 9, tzinfo=UTC),
    status="OPEN",
    lines=(
        SourceOrderLine(1, "SKU-001", 3, 0, "ACTIVE"),
        SourceOrderLine(2, "SKU-001", 2, 2, "ACTIVE"),
    ),
)
SHIPMENT = SourceShipment(
    shipment_code="OCN-1",
    transport_mode="OCEAN",
    warehouse_code="TMS-LAX",
    planned_kr_release_date=date(2026, 1, 2),
    current_stage="AT_SEA",
    current_expected_port_arrival_date=date(2026, 1, 22),
    current_expected_3pl_inbound_date=date(2026, 1, 27),
    actual_3pl_inbound_date=None,
    lines=(SourceShipmentLine(1, "TMS-SKU-001", 300),),
)
INVENTORY = SourceInventory(
    inventory_date=date(2026, 1, 5),
    warehouse_code="WMS-EWR01",
    sku_code="WMS-SKU-001",
    on_hand_quantity=120,
    reserved_quantity=20,
    available_quantity=100,
    outbound_quantity=7,
)
INVENTORY_KEY = "WMS-EWR01|SKU-001|2026-01-05"


def _check(rejection):
    return (rejection.source_record_key, rejection.field_name, rejection.error_code)


def test_valid_records_pass():
    assert validate_sku(SKU) is None
    assert validate_warehouse(WAREHOUSE) is None
    assert validate_order(ORDER) is None
    assert validate_shipment(SHIPMENT) is None
    assert validate_inventory(INVENTORY, INVENTORY_KEY) is None


@pytest.mark.parametrize(
    ("change", "field_name", "error_code"),
    [
        ({"popularity_tier": "TOP"}, "popularity_tier", INVALID_VALUE),  # skus_ck1
        ({"units_per_case": 0}, "units_per_case", INVALID_QUANTITY),  # skus_ck2
        ({"units_per_case": -1}, "units_per_case", NEGATIVE_QUANTITY),
        ({"status": "DELETED"}, "status", INVALID_VALUE),  # skus_ck3
    ],
)
def test_sku_rejections(change, field_name, error_code):
    rejection = validate_sku(replace(SKU, **change))

    assert _check(rejection) == ("SKU-001", field_name, error_code)


@pytest.mark.parametrize(
    ("change", "field_name"),
    [
        ({"warehouse_type": "DC"}, "warehouse_type"),  # warehouses_ck1
        ({"region_code": "SOUTH"}, "region_code"),  # warehouses_ck2
    ],
)
def test_warehouse_rejections(change, field_name):
    rejection = validate_warehouse(replace(WAREHOUSE, **change))

    assert _check(rejection) == ("WH-WEST", field_name, INVALID_VALUE)


@pytest.mark.parametrize(
    ("change", "field_name"),
    [
        ({"customer_region": "KR"}, "customer_region"),  # orders_ck1
        ({"sales_channel": "EBAY"}, "sales_channel"),  # orders_ck2
        ({"status": "LOST"}, "status"),  # orders_ck3
    ],
)
def test_order_header_rejections(change, field_name):
    rejection = validate_order(replace(ORDER, **change))

    assert _check(rejection) == ("ORD-1", field_name, INVALID_VALUE)


@pytest.mark.parametrize(
    ("line", "field_name", "error_code"),
    [
        # order_lines_ck1: 주문 수량 > 0 (기준선 B-ORD-ERR-0004 처럼 음수는 NEGATIVE_QUANTITY)
        (
            SourceOrderLine(2, "SKU-001", -1, 0, "ACTIVE"),
            "ordered_quantity",
            NEGATIVE_QUANTITY,
        ),
        (
            SourceOrderLine(2, "SKU-001", 0, 0, "ACTIVE"),
            "ordered_quantity",
            INVALID_QUANTITY,
        ),
        # order_lines_ck2: 0 ≤ 출고 ≤ 주문
        (
            SourceOrderLine(2, "SKU-001", 2, -1, "ACTIVE"),
            "shipped_quantity",
            NEGATIVE_QUANTITY,
        ),
        (
            SourceOrderLine(2, "SKU-001", 2, 3, "ACTIVE"),
            "shipped_quantity",
            INCONSISTENT_VALUE,
        ),
        # order_lines_ck3
        (SourceOrderLine(2, "SKU-001", 2, 0, "DELETED"), "line_status", INVALID_VALUE),
    ],
)
def test_order_line_rejections_point_at_the_line(line, field_name, error_code):
    rejection = validate_order(replace(ORDER, lines=(ORDER.lines[0], line)))

    assert _check(rejection) == ("ORD-1", f"lines[1].{field_name}", error_code)


@pytest.mark.parametrize(
    ("change", "field_name", "error_code"),
    [
        ({"current_stage": "LOST"}, "current_stage", INVALID_VALUE),  # shipments_ck2
        # shipments_ck3: RECEIVED ⇔ 실제 입고일
        ({"current_stage": "RECEIVED"}, "actual_3pl_inbound_date", INCONSISTENT_VALUE),
        (
            {"actual_3pl_inbound_date": date(2026, 1, 27)},
            "actual_3pl_inbound_date",
            INCONSISTENT_VALUE,
        ),
        # shipment_lines_ck1
        (
            {"lines": (SourceShipmentLine(1, "TMS-SKU-001", 0),)},
            "lines[0].shipped_quantity",
            INVALID_QUANTITY,
        ),
    ],
)
def test_shipment_rejections(change, field_name, error_code):
    rejection = validate_shipment(replace(SHIPMENT, **change))

    assert _check(rejection) == ("OCN-1", field_name, error_code)


def test_received_shipment_with_inbound_date_passes():
    received = replace(
        SHIPMENT, current_stage="RECEIVED", actual_3pl_inbound_date=date(2026, 1, 27)
    )

    assert validate_shipment(received) is None


@pytest.mark.parametrize(
    ("change", "field_name", "error_code"),
    [
        # inventory_daily_ck1~4 (기준선 WMS-EWR01|SKU-215|2026-01-01 on_hand_quantity NEGATIVE_QUANTITY)
        (
            {"on_hand_quantity": -1, "available_quantity": -21},
            "on_hand_quantity",
            NEGATIVE_QUANTITY,
        ),
        ({"reserved_quantity": -1}, "reserved_quantity", NEGATIVE_QUANTITY),
        ({"available_quantity": -1}, "available_quantity", NEGATIVE_QUANTITY),
        ({"outbound_quantity": -1}, "outbound_quantity", NEGATIVE_QUANTITY),
        # inventory_daily_ck5 (ck6·ck7 은 음수가 아니면 ck5 가 보장)
        ({"available_quantity": 99}, "on_hand_quantity", INCONSISTENT_VALUE),
    ],
)
def test_inventory_rejections(change, field_name, error_code):
    rejection = validate_inventory(replace(INVENTORY, **change), INVENTORY_KEY)

    assert _check(rejection) == (INVENTORY_KEY, field_name, error_code)


def test_first_broken_value_is_the_reason():
    """한 레코드에 여러 값이 어긋나도 거부는 1건 (앞쪽 필드)"""
    order = replace(ORDER, customer_region="KR", sales_channel="EBAY")

    assert validate_order(order).field_name == "customer_region"


# --- 허용 값 목록이 DB 제약과 같은지 -----------------------------------------------------


def _check_values(model, name: str) -> set[str]:
    [constraint] = [
        c
        for c in model.__table__.constraints
        if isinstance(c, CheckConstraint) and c.name == name
    ]
    return set(re.findall(r"'([^']+)'", str(constraint.sqltext)))


@pytest.mark.parametrize(
    ("model", "name", "allowed"),
    [
        (Sku, "skus_ck1", POPULARITY_TIERS),
        (Sku, "skus_ck3", SKU_STATUSES),
        (Warehouse, "warehouses_ck1", WAREHOUSE_TYPES),
        (Warehouse, "warehouses_ck2", REGION_CODES),
        (Order, "orders_ck1", CUSTOMER_REGIONS),
        (Order, "orders_ck2", SALES_CHANNELS),
        (Order, "orders_ck3", ORDER_STATUSES),
        (OrderLine, "order_lines_ck3", LINE_STATUSES),
        (Shipment, "shipments_ck2", SHIPMENT_STAGES),
    ],
)
def test_allowed_values_match_orm_check_constraints(model, name, allowed):
    assert _check_values(model, name) == allowed
