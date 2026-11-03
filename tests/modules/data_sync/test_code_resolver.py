import uuid
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from app.modules.data_sync.application.ingest.code_resolver import (
    DUPLICATE_LINE_NO,
    NO_LEADTIME_REFERENCE,
    NO_ROUTE_PORT,
    UNKNOWN_SKU,
    UNKNOWN_WAREHOUSE,
    UNSUPPORTED_TRANSPORT_MODE,
    CodeBook,
    LeadtimeReference,
    Rejection,
    ShipmentPlan,
    SkuCode,
    WarehouseCode,
    find_leadtime_reference,
    inventory_record_key,
    planned_3pl_inbound_date,
    resolve_inventory,
    resolve_order,
    resolve_shipment,
)
from app.modules.data_sync.application.ingest.source_payload import (
    SourceInventory,
    SourceOrder,
    SourceOrderLine,
    SourceShipment,
    SourceShipmentLine,
)

SKU_ID = uuid.uuid7()
WEST_ID = uuid.uuid7()
EAST_ID = uuid.uuid7()
OCEAN_JAN = LeadtimeReference(uuid.uuid7(), 24.35)
BOOK = CodeBook(
    skus={"TMS-SKU-001": SkuCode(SKU_ID, "SKU-001")},
    warehouses={
        "TMS-LAX": WarehouseCode(WEST_ID, "WH-WEST", "WEST"),
        "TMS-EWR": WarehouseCode(EAST_ID, "WH-EAST", "EAST"),
    },
    leadtime_references={("2026-01", "2704", "vessel_container"): OCEAN_JAN},
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
ORDER = SourceOrder(
    order_code="ORD-1",
    sales_channel="AMAZON",
    customer_region="WEST",
    warehouse_code="TMS-LAX",
    ordered_at=datetime(2026, 1, 5, tzinfo=UTC),
    promised_delivery_at=datetime(2026, 1, 9, tzinfo=UTC),
    status="OPEN",
    lines=(SourceOrderLine(1, "TMS-SKU-001", 3, 0, "ACTIVE"),),
)
INVENTORY = SourceInventory(
    inventory_date=date(2026, 1, 5),
    warehouse_code="TMS-LAX",
    sku_code="TMS-SKU-001",
    on_hand_quantity=120,
    reserved_quantity=20,
    available_quantity=100,
    outbound_quantity=7,
)


def _refs(*months: str, port="2704", mode="vessel_container"):
    return {(m, port, mode): LeadtimeReference(uuid.uuid7(), 20.0) for m in months}


# --- 계획 3PL 입고일 ---------------------------------------------------------------


def test_planned_inbound_date_rounds_p90_up():
    assert planned_3pl_inbound_date(date(2026, 1, 2), 24.35) == date(2026, 1, 27)
    assert planned_3pl_inbound_date(date(2026, 1, 2), 25.0) == date(2026, 1, 27)


# --- 시장 리드타임 기준 찾기 ---------------------------------------------------------


def test_reference_of_release_month_comes_first():
    refs = _refs("2025-02", "2026-01")

    found = find_leadtime_reference(refs, date(2026, 1, 9), "2704", "vessel_container")

    assert found is refs[("2026-01", "2704", "vessel_container")]


def test_after_last_accepted_month_uses_same_month_of_latest_year():
    refs = _refs(
        "2024-03", "2025-03", "2026-02", "2026-03"
    )  # 2026-02·03 은 인정 범위 밖

    found = find_leadtime_reference(refs, date(2026, 3, 5), "2704", "vessel_container")

    assert found is refs[("2025-03", "2704", "vessel_container")]


def test_without_same_month_uses_nearest_previous_month():
    refs = _refs("2025-01", "2025-11")

    found = find_leadtime_reference(refs, date(2026, 3, 5), "2704", "vessel_container")

    assert found is refs[("2025-11", "2704", "vessel_container")]


def test_reference_of_other_route_or_future_is_not_used():
    refs = _refs("2026-01", port="1003") | _refs("2026-01", mode="air")

    assert (
        find_leadtime_reference(refs, date(2026, 1, 9), "2704", "vessel_container")
        is None
    )
    assert (
        find_leadtime_reference(
            _refs("2025-12"), date(2025, 11, 9), "2704", "vessel_container"
        )
        is None
    )


# --- 선적 ----------------------------------------------------------------------------


def test_new_shipment_gets_port_reference_and_planned_date():
    record = resolve_shipment(SHIPMENT, BOOK)

    assert record.warehouse_id == WEST_ID
    assert record.market_leadtime_reference_id == OCEAN_JAN.market_leadtime_reference_id
    assert record.planned_3pl_inbound_date == date(2026, 1, 27)
    assert record.lines[0].sku_id == SKU_ID


@pytest.mark.parametrize(
    ("warehouse_code", "mode", "port", "reference_mode"),
    [
        ("TMS-LAX", "OCEAN", "2704", "vessel_container"),
        ("TMS-EWR", "AIR", "1003", "air"),
        ("TMS-LAX", "AIR", "2720", "air"),
    ],
)
def test_route_table_decides_port(warehouse_code, mode, port, reference_mode):
    reference = LeadtimeReference(uuid.uuid7(), 5.0)
    book = replace(
        BOOK, leadtime_references={("2026-01", port, reference_mode): reference}
    )

    record = resolve_shipment(
        replace(SHIPMENT, warehouse_code=warehouse_code, transport_mode=mode), book
    )

    assert record.market_leadtime_reference_id == reference.market_leadtime_reference_id


def test_existing_shipment_keeps_stored_plan():
    plan = ShipmentPlan(uuid.uuid7(), date(2026, 2, 1))

    record = resolve_shipment(
        replace(SHIPMENT, planned_kr_release_date=date(2026, 1, 9)), BOOK, plan
    )

    assert record.market_leadtime_reference_id == plan.market_leadtime_reference_id
    assert record.planned_3pl_inbound_date == date(2026, 2, 1)
    assert record.planned_kr_release_date == date(2026, 1, 9)


def test_existing_shipment_does_not_need_a_reference():
    plan = ShipmentPlan(uuid.uuid7(), date(2026, 2, 1))
    no_refs = replace(BOOK, leadtime_references={})

    assert not isinstance(resolve_shipment(SHIPMENT, no_refs, plan), Rejection)


@pytest.mark.parametrize(
    ("change", "field_name", "error_code"),
    [
        ({"transport_mode": "SEA"}, "transport_mode", UNSUPPORTED_TRANSPORT_MODE),
        ({"warehouse_code": "WH-WEST"}, "warehouse_code", UNKNOWN_WAREHOUSE),
        (
            {"warehouse_code": "TMS-EWR", "transport_mode": "OCEAN"},
            "warehouse_code",
            NO_ROUTE_PORT,
        ),
        (
            {"planned_kr_release_date": date(2025, 12, 2)},
            "planned_kr_release_date",
            NO_LEADTIME_REFERENCE,
        ),
    ],
)
def test_shipment_rejections(change, field_name, error_code):
    rejection = resolve_shipment(replace(SHIPMENT, **change), BOOK)

    assert isinstance(rejection, Rejection)
    assert (
        rejection.source_record_key,
        rejection.field_name,
        rejection.error_code,
    ) == (
        "OCN-1",
        field_name,
        error_code,
    )


def test_unmapped_line_sku_rejects_the_whole_record():
    shipment = replace(
        SHIPMENT, lines=(*SHIPMENT.lines, SourceShipmentLine(2, "TMS-SKU-404", 5))
    )
    order = replace(ORDER, lines=(SourceOrderLine(2, "UNKNOWN", 1, 0, "ACTIVE"),))

    # 키는 부모 코드, 상세 위치는 field_name 의 lines[i] (기준선 B-ORD-ERR-0001 · lines[0].sellerSku 와 같은 방식)
    for rejection, key, field_name in (
        (resolve_shipment(shipment, BOOK), "OCN-1", "lines[1].sku_code"),
        (resolve_order(order, BOOK), "ORD-1", "lines[0].sku_code"),
    ):
        assert (
            rejection.source_record_key,
            rejection.field_name,
            rejection.error_code,
        ) == (
            key,
            field_name,
            UNKNOWN_SKU,
        )


# --- 주문·재고 -------------------------------------------------------------------------


def test_order_resolves_ids():
    record = resolve_order(ORDER, BOOK)

    assert record.warehouse_id == WEST_ID
    assert record.lines[0].sku_id == SKU_ID


def test_unmapped_order_warehouse_is_rejected():
    rejection = resolve_order(replace(ORDER, warehouse_code="WH-404"), BOOK)

    assert (rejection.field_name, rejection.error_code) == (
        "warehouse_code",
        UNKNOWN_WAREHOUSE,
    )
    assert "WH-404" in rejection.error_message


def test_inventory_uses_internal_codes_for_summary_labels():
    record = resolve_inventory(INVENTORY, BOOK)

    assert (record.warehouse_id, record.sku_id) == (WEST_ID, SKU_ID)
    assert (record.warehouse_code, record.sku_code) == ("WH-WEST", "SKU-001")


@pytest.mark.parametrize(
    ("change", "key", "field_name", "error_code"),
    [
        # SKU 매핑 없음: SKU 자리는 원천 외부 코드
        ({"sku_code": "X"}, "TMS-LAX|X|2026-01-05", "sku_code", UNKNOWN_SKU),
        # 창고 매핑 없음: 창고 자리는 외부 코드, 매핑된 SKU 는 내부 코드 (기준선 WMS-ATL01|SKU-201|2025-12-31)
        (
            {"warehouse_code": "WMS-ATL01"},
            "WMS-ATL01|SKU-001|2026-01-05",
            "warehouse_code",
            UNKNOWN_WAREHOUSE,
        ),
    ],
)
def test_inventory_rejection_key_matches_baseline(change, key, field_name, error_code):
    rejection = resolve_inventory(replace(INVENTORY, **change), BOOK)

    assert (
        rejection.source_record_key,
        rejection.field_name,
        rejection.error_code,
    ) == (key, field_name, error_code)


def test_inventory_key_keeps_external_code_for_mapped_warehouse():
    """매핑된 창고도 창고 자리는 외부 코드 (기준선 WMS-EWR01|SKU-215|2026-01-01, 검증 엔진 snapshot.py 의 외부 코드 조회)"""
    assert inventory_record_key(INVENTORY, BOOK) == "TMS-LAX|SKU-001|2026-01-05"


# --- 상세 줄 번호 중복 -----------------------------------------------------------------


def test_duplicate_order_line_no_rejects_the_order_once():
    order = replace(
        ORDER,
        lines=(
            SourceOrderLine(1, "TMS-SKU-001", 3, 0, "ACTIVE"),
            SourceOrderLine(1, "TMS-SKU-001", 2, 0, "ACTIVE"),
            SourceOrderLine(2, "TMS-SKU-001", 1, 0, "ACTIVE"),
            SourceOrderLine(2, "TMS-SKU-001", 1, 0, "ACTIVE"),
            SourceOrderLine(2, "TMS-SKU-001", 1, 0, "ACTIVE"),
        ),
    )

    rejection = resolve_order(order, BOOK)

    assert isinstance(rejection, Rejection)
    assert (
        rejection.source_record_key,
        rejection.field_name,
        rejection.error_code,
    ) == (
        "ORD-1",
        "line_no",
        DUPLICATE_LINE_NO,
    )
    assert "1, 2" in rejection.error_message


def test_duplicate_shipment_line_no_rejects_the_shipment():
    shipment = replace(
        SHIPMENT,
        lines=(
            SourceShipmentLine(1, "TMS-SKU-001", 300),
            SourceShipmentLine(1, "TMS-SKU-001", 10),
        ),
    )

    rejection = resolve_shipment(shipment, BOOK)

    assert (rejection.source_record_key, rejection.error_code) == (
        "OCN-1",
        DUPLICATE_LINE_NO,
    )


def test_duplicate_line_no_is_checked_before_code_mapping():
    """줄 번호가 겹치면 SKU 매핑 실패보다 먼저 같은 사유 1건으로 거부"""
    order = replace(
        ORDER,
        lines=(
            SourceOrderLine(1, "UNKNOWN", 1, 0, "ACTIVE"),
            SourceOrderLine(1, "UNKNOWN", 1, 0, "ACTIVE"),
        ),
    )

    assert resolve_order(order, BOOK).error_code == DUPLICATE_LINE_NO
