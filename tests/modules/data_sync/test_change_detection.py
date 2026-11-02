import uuid
from dataclasses import replace
from datetime import UTC, date, datetime

from app.modules.data_sync.application.ingest.change_detection import (
    Existing,
    ExistingOrder,
    ExistingShipment,
    diff_inventory,
    diff_order,
    diff_shipment,
    diff_sku,
    diff_warehouse,
)
from app.modules.data_sync.application.ingest.source_records import (
    InventoryRecord,
    OrderLineRecord,
    OrderRecord,
    ShipmentLineRecord,
    ShipmentRecord,
    SkuRecord,
    WarehouseRecord,
)
from app.modules.delay_analysis.application.stale.relevance import (
    _PLANNED_SHIPMENT_SUMMARIES,
)

WAREHOUSE_ID = uuid.uuid7()
SKU_A = uuid.uuid7()
SKU_B = uuid.uuid7()
SKU_C = uuid.uuid7()

SKU = SkuRecord(
    sku_code="SKU-001",
    product_name="세라마이드 크림",
    brand_name="GLOW",
    hs10="3304990000",
    category="SKINCARE",
    subcategory="CREAM",
    popularity_tier="HIT",
    unit_code="EA",
    units_per_case=24,
    emergency_air_eligible=True,
    status="ACTIVE",
)
WAREHOUSE = WarehouseRecord(
    warehouse_code="WH-WEST",
    warehouse_name="West 3PL",
    warehouse_type="US_3PL",
    region_code="WEST",
)
ORDER = OrderRecord(
    order_code="ORD-1",
    sales_channel="AMAZON",
    customer_region="WEST",
    warehouse_id=WAREHOUSE_ID,
    ordered_at=datetime(2026, 1, 5, tzinfo=UTC),
    promised_delivery_at=datetime(2026, 1, 9, tzinfo=UTC),
    status="OPEN",
    lines=(
        OrderLineRecord(1, SKU_A, 3, 0, "ACTIVE"),
        OrderLineRecord(2, SKU_B, 1, 0, "ACTIVE"),
    ),
)
SHIPMENT = ShipmentRecord(
    shipment_code="OCN-1",
    transport_mode="OCEAN",
    warehouse_id=WAREHOUSE_ID,
    market_leadtime_reference_id=uuid.uuid7(),
    planned_kr_release_date=date(2026, 1, 2),
    planned_3pl_inbound_date=date(2026, 1, 27),
    current_stage="AT_SEA",
    current_expected_port_arrival_date=date(2026, 1, 22),
    current_expected_3pl_inbound_date=date(2026, 1, 27),
    actual_3pl_inbound_date=None,
    lines=(ShipmentLineRecord(1, SKU_A, 300), ShipmentLineRecord(2, SKU_B, 100)),
)
INVENTORY = InventoryRecord(
    inventory_date=date(2026, 1, 5),
    warehouse_id=WAREHOUSE_ID,
    sku_id=SKU_A,
    on_hand_quantity=120,
    reserved_quantity=20,
    available_quantity=100,
    outbound_quantity=7,
    warehouse_code="WH-WEST",
    sku_code="SKU-001",
)


def _existing_order(record: OrderRecord = ORDER) -> ExistingOrder:
    return ExistingOrder(
        uuid.uuid7(), record, {line.line_no: uuid.uuid7() for line in record.lines}
    )


def _existing_shipment(record: ShipmentRecord = SHIPMENT) -> ExistingShipment:
    return ExistingShipment(
        uuid.uuid7(), record, {line.line_no: uuid.uuid7() for line in record.lines}
    )


# --- 같은 값 재수집 ----------------------------------------------------------------


def test_same_values_produce_no_change():
    assert diff_sku(Existing(uuid.uuid7(), SKU), SKU) is None
    assert diff_warehouse(Existing(uuid.uuid7(), WAREHOUSE), WAREHOUSE) is None
    assert diff_inventory(Existing(uuid.uuid7(), INVENTORY), INVENTORY) is None
    assert diff_order(_existing_order(), ORDER) is None
    assert diff_shipment(_existing_shipment(), SHIPMENT) is None


def test_surrounding_spaces_are_not_a_change():
    assert (
        diff_sku(
            Existing(uuid.uuid7(), SKU), replace(SKU, product_name=" 세라마이드 크림 ")
        )
        is None
    )


def test_label_codes_are_not_compared():
    moved = replace(INVENTORY, warehouse_code="WH-OTHER")

    assert diff_inventory(Existing(uuid.uuid7(), INVENTORY), moved) is None


# --- 상품·창고·재고 ---------------------------------------------------------------


def test_new_sku_is_insert_with_new_id_and_no_changed_fields():
    diff = diff_sku(None, SKU)

    assert diff.row.change_type == "INSERT"
    [record] = diff.records
    assert record.entity_ref == record.sku_id == diff.row.row_id
    assert record.warehouse_id is None
    assert record.changed_fields is None
    assert record.change_summary == "상품 SKU-001"


def test_sku_update_lists_changed_columns():
    row_id = uuid.uuid7()
    changed = replace(SKU, popularity_tier="REGULAR", units_per_case=12)

    diff = diff_sku(Existing(row_id, SKU), changed)

    assert diff.row.row_id == row_id
    assert diff.row.changed_fields == ("popularity_tier", "units_per_case")
    [record] = diff.records
    assert record.changed_fields == ("popularity_tier", "units_per_case")
    assert record.change_summary == "상품 SKU-001 변경: popularity_tier, units_per_case"


def test_warehouse_change_is_recorded_on_the_warehouse():
    diff = diff_warehouse(
        Existing(uuid.uuid7(), WAREHOUSE), replace(WAREHOUSE, region_code="EAST")
    )

    [record] = diff.records
    assert (record.entity_type, record.sku_id) == ("WAREHOUSE", None)
    assert record.warehouse_id == diff.row.row_id


def test_inventory_insert_summary_uses_date_warehouse_and_sku_codes():
    [record] = diff_inventory(None, INVENTORY).records

    assert record.change_summary == "재고 2026-01-05 WH-WEST SKU-001"
    assert (record.sku_id, record.warehouse_id) == (SKU_A, WAREHOUSE_ID)


# --- 주문 -------------------------------------------------------------------------


def test_new_order_records_header_per_sku_and_each_line():
    diff = diff_order(None, ORDER)

    assert diff.header.change_type == "INSERT"
    assert diff.order_id == diff.header.row_id
    assert [line.change_type for line in diff.lines] == ["INSERT", "INSERT"]
    summaries = [(r.entity_type, r.sku_id, r.change_summary) for r in diff.records]
    assert summaries == [
        ("ORDER", SKU_A, "주문 ORD-1"),
        ("ORDER", SKU_B, "주문 ORD-1"),
        ("ORDER_LINE", SKU_A, "주문 상세 ORD-1#1"),
        ("ORDER_LINE", SKU_B, "주문 상세 ORD-1#2"),
    ]
    assert all(r.warehouse_id == WAREHOUSE_ID for r in diff.records)


def test_status_only_change_uses_planned_shipment_summary():
    existing = _existing_order()

    diff = diff_order(existing, replace(ORDER, status="SHIPPED"))

    assert diff.lines == ()
    assert {r.change_summary for r in diff.records} == {"주문 상태 SHIPPED"}
    assert [r.sku_id for r in diff.records] == [SKU_A, SKU_B]
    assert all(r.entity_ref == existing.row_id for r in diff.records)


def test_shipped_quantity_change_uses_planned_shipment_summary():
    existing = _existing_order()
    shipped = replace(
        ORDER, lines=(replace(ORDER.lines[0], shipped_quantity=3), ORDER.lines[1])
    )

    diff = diff_order(existing, shipped)

    assert (
        diff.header is None
    )  # 머리 값은 그대로지만 상세가 바뀜 → 주문 수집 ID 는 서비스가 교체
    [line] = diff.lines
    assert (line.row_id, line.changed_fields) == (
        existing.line_ids[1],
        ("shipped_quantity",),
    )
    [record] = diff.records
    assert (record.entity_type, record.change_summary) == ("ORDER_LINE", "출고 수량")


def test_planned_shipment_summaries_match_stale_contract():
    """STALE 판정이 정상 출고로 보는 요약 문장과 같은 문장을 만든다 (relevance._PLANNED_SHIPMENT_SUMMARIES)"""
    existing = _existing_order()
    produced = {
        r.change_summary
        for status in ("SHIPPED", "DELIVERED")
        for r in diff_order(existing, replace(ORDER, status=status)).records
    }
    shipped = replace(
        ORDER, lines=(replace(ORDER.lines[0], shipped_quantity=3), ORDER.lines[1])
    )
    produced_lines = {r.change_summary for r in diff_order(existing, shipped).records}

    assert produced == _PLANNED_SHIPMENT_SUMMARIES["ORDER"]
    assert produced_lines == _PLANNED_SHIPMENT_SUMMARIES["ORDER_LINE"]


def test_cancellation_is_not_a_planned_shipment_summary():
    diff = diff_order(_existing_order(), replace(ORDER, status="CANCELLED"))

    assert {r.change_summary for r in diff.records} == {"주문 상태 CANCELLED"}
    assert "주문 상태 CANCELLED" not in _PLANNED_SHIPMENT_SUMMARIES["ORDER"]


def test_header_change_with_several_fields_lists_them():
    diff = diff_order(
        _existing_order(),
        replace(ORDER, status="CANCELLED", customer_region="EAST"),
    )

    assert {r.change_summary for r in diff.records} == {
        "주문 ORD-1 변경: customer_region, status"
    }
    assert diff.records[0].changed_fields == ("customer_region", "status")


def test_line_sku_change_records_both_skus():
    existing = _existing_order()
    swapped = replace(
        ORDER, lines=(replace(ORDER.lines[0], sku_id=SKU_C), ORDER.lines[1])
    )

    diff = diff_order(existing, swapped)

    assert [(r.sku_id, r.changed_fields) for r in diff.records] == [
        (SKU_A, ("sku_id",)),
        (SKU_C, ("sku_id",)),
    ]
    assert {r.change_summary for r in diff.records} == {
        "주문 상세 ORD-1#1 변경: sku_id"
    }


def test_header_change_covers_skus_before_and_after_line_changes():
    existing = _existing_order()
    changed = replace(
        ORDER,
        status="CANCELLED",
        lines=(replace(ORDER.lines[0], sku_id=SKU_C), ORDER.lines[1]),
    )

    diff = diff_order(existing, changed)

    header_skus = [r.sku_id for r in diff.records if r.entity_type == "ORDER"]
    assert header_skus == [SKU_A, SKU_B, SKU_C]


def test_removed_at_source_is_a_status_update():
    existing = _existing_order()
    removed = replace(
        ORDER,
        status="REMOVED_AT_SOURCE",
        lines=tuple(
            replace(line, line_status="REMOVED_AT_SOURCE") for line in ORDER.lines
        ),
    )

    diff = diff_order(existing, removed)

    assert diff.header.change_type == "UPDATE"
    assert {r.change_type for r in diff.records} == {"UPDATE"}
    line_summaries = {
        r.change_summary for r in diff.records if r.entity_type == "ORDER_LINE"
    }
    assert line_summaries == {
        "주문 상세 ORD-1#1 변경: line_status",
        "주문 상세 ORD-1#2 변경: line_status",
    }


def test_lines_missing_from_source_are_not_removed():
    existing = _existing_order()
    only_first = replace(ORDER, lines=ORDER.lines[:1])

    assert diff_order(existing, only_first) is None


def test_new_line_on_existing_order_is_insert():
    existing = _existing_order()
    added = replace(
        ORDER, lines=(*ORDER.lines, OrderLineRecord(3, SKU_C, 2, 0, "ACTIVE"))
    )

    diff = diff_order(existing, added)

    [line] = diff.lines
    assert line.change_type == "INSERT"
    assert line.row_id not in existing.line_ids.values()
    [record] = diff.records
    assert (record.sku_id, record.change_summary) == (SKU_C, "주문 상세 ORD-1#3")


# --- 선적 -------------------------------------------------------------------------


def test_new_shipment_takes_a_schedule_snapshot():
    diff = diff_shipment(None, SHIPMENT)

    assert diff.schedule_changed
    header_records = [r for r in diff.records if r.entity_type == "SHIPMENT"]
    assert [r.sku_id for r in header_records] == [SKU_A, SKU_B]
    assert {r.change_summary for r in header_records} == {"선적 OCN-1 AT_SEA"}
    line_summaries = [
        r.change_summary for r in diff.records if r.entity_type == "SHIPMENT_LINE"
    ]
    assert line_summaries == ["선적 상세 OCN-1#1", "선적 상세 OCN-1#2"]


def test_expected_date_change_takes_a_snapshot():
    diff = diff_shipment(
        _existing_shipment(),
        replace(SHIPMENT, current_expected_3pl_inbound_date=date(2026, 1, 30)),
    )

    assert diff.schedule_changed
    assert diff.header.changed_fields == ("current_expected_3pl_inbound_date",)
    assert {r.change_summary for r in diff.records} == {"선적 OCN-1 AT_SEA"}


def test_stage_change_takes_a_snapshot_and_summary_shows_new_stage():
    diff = diff_shipment(
        _existing_shipment(), replace(SHIPMENT, current_stage="AT_PORT")
    )

    assert diff.schedule_changed
    assert {r.change_summary for r in diff.records} == {"선적 OCN-1 AT_PORT"}


def test_actual_inbound_only_does_not_take_a_snapshot():
    diff = diff_shipment(
        _existing_shipment(),
        replace(SHIPMENT, actual_3pl_inbound_date=date(2026, 1, 28)),
    )

    assert not diff.schedule_changed
    assert diff.header.changed_fields == ("actual_3pl_inbound_date",)


def test_shipment_line_quantity_change_keeps_header_and_snapshot():
    existing = _existing_shipment()
    changed = replace(
        SHIPMENT,
        lines=(replace(SHIPMENT.lines[0], shipped_quantity=250), SHIPMENT.lines[1]),
    )

    diff = diff_shipment(existing, changed)

    assert diff.header is None
    assert not diff.schedule_changed
    [record] = diff.records
    assert record.entity_type == "SHIPMENT_LINE"
    assert record.change_summary == "선적 상세 OCN-1#1 변경: shipped_quantity"
    assert record.entity_ref == existing.line_ids[1]
