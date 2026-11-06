from datetime import date

import pytest

from app.modules.data_sync.application.ingest.code_resolver import (
    MISSING_REQUIRED,
    Rejection,
)
from app.modules.data_sync.application.ingest.source_payload import (
    SourceShipment,
    SourceShipmentLine,
)
from app.modules.data_sync.application.ingest.tms_parser import (
    INVALID_DATE,
    INVALID_FORMAT,
    parse_tms_shipments,
)

"""
    TMS 선적 응답 파서 (source_payload 의 파서 계약 1~4)
      - 정상 응답은 census_kbeauty source_samples/shipments_api.json 의 선적 2건과 같은 모양입니다.
"""

RECEIVED = {
    "_demo": {"case": "VALID"},
    "shipmentNo": "SHP-NORMAL-001",
    "mode": "OCEAN",
    "destWh": "TMS-LAX",
    "plannedKrRelease": "2025-01-05",
    "stage": "RECEIVED",
    "etaPort": "2025-01-30",
    "eta3pl": "2025-02-03",
    "actual3pl": "2025-02-03",
    "items": [{"lineNo": 1, "sku": "TMS-SKU-001", "qty": 240}],
}
AT_SEA = {
    "shipmentNo": "SHP-DELAY-002",
    "mode": "AIR",
    "destWh": "TMS-NJ",
    "plannedKrRelease": "2025-01-22",
    "stage": "AT_SEA",
    "etaPort": None,
    "eta3pl": "2025-02-19",
    "actual3pl": None,
    "items": [
        {"lineNo": 1, "sku": "TMS-SKU-002", "qty": 120},
        {"lineNo": 2, "sku": "TMS-SKU-003", "qty": 60},
    ],
}


def _with(base: dict, **changes) -> dict:
    return base | changes


def test_parses_tms_fields_into_source_shipments():
    result = parse_tms_shipments([RECEIVED, AT_SEA])

    assert result.rejections == []
    assert result.shipments == [
        SourceShipment(
            shipment_code="SHP-NORMAL-001",
            transport_mode="OCEAN",
            warehouse_code="TMS-LAX",
            planned_kr_release_date=date(2025, 1, 5),
            current_stage="RECEIVED",
            current_expected_port_arrival_date=date(2025, 1, 30),
            current_expected_3pl_inbound_date=date(2025, 2, 3),
            actual_3pl_inbound_date=date(2025, 2, 3),
            lines=(SourceShipmentLine(1, "TMS-SKU-001", 240),),
        ),
        SourceShipment(
            shipment_code="SHP-DELAY-002",
            transport_mode="AIR",
            warehouse_code="TMS-NJ",
            planned_kr_release_date=date(2025, 1, 22),
            current_stage="AT_SEA",
            current_expected_port_arrival_date=None,
            current_expected_3pl_inbound_date=date(2025, 2, 19),
            actual_3pl_inbound_date=None,
            lines=(
                SourceShipmentLine(1, "TMS-SKU-002", 120),
                SourceShipmentLine(2, "TMS-SKU-003", 60),
            ),
        ),
    ]


def test_optional_dates_may_be_absent():
    without = {k: v for k, v in AT_SEA.items() if k not in ("etaPort", "actual3pl")}

    [shipment] = parse_tms_shipments([without]).shipments

    assert shipment.current_expected_port_arrival_date is None
    assert shipment.actual_3pl_inbound_date is None


def test_meaning_is_left_to_ingest_validation():
    """단계 값·운송 수단·수량 양수·빈 상세는 파서가 아니라 적재 서비스가 거부한다 (계약 끝 문단)"""
    odd = _with(AT_SEA, mode="RAIL", stage="LOST", items=[])
    negative = _with(
        RECEIVED, shipmentNo="SHP-3", items=[{"lineNo": 1, "sku": "X", "qty": -5}]
    )

    result = parse_tms_shipments([odd, negative])

    assert result.rejections == []
    assert [s.transport_mode for s in result.shipments] == ["RAIL", "OCEAN"]
    assert result.shipments[1].lines[0].shipped_quantity == -5


@pytest.mark.parametrize(
    ("record", "field_name", "error_code"),
    [
        (_with(AT_SEA, shipmentNo=None), "shipmentNo", MISSING_REQUIRED),
        (_with(AT_SEA, mode=None), "mode", MISSING_REQUIRED),
        (_with(AT_SEA, destWh="  "), "destWh", MISSING_REQUIRED),
        (_with(AT_SEA, stage=None), "stage", MISSING_REQUIRED),
        (_with(AT_SEA, plannedKrRelease=None), "plannedKrRelease", MISSING_REQUIRED),
        (_with(AT_SEA, eta3pl=None), "eta3pl", MISSING_REQUIRED),
        (_with(AT_SEA, items=None), "items", MISSING_REQUIRED),
        (_with(AT_SEA, eta3pl="2025/02/19"), "eta3pl", INVALID_DATE),
        (_with(AT_SEA, eta3pl="2025-02-30"), "eta3pl", INVALID_DATE),
        (_with(AT_SEA, eta3pl="2025-02-19T00:00:00Z"), "eta3pl", INVALID_DATE),
        (_with(AT_SEA, plannedKrRelease=20250122), "plannedKrRelease", INVALID_DATE),
        (_with(AT_SEA, etaPort="soon"), "etaPort", INVALID_DATE),
        (_with(AT_SEA, actual3pl=""), "actual3pl", INVALID_DATE),
        (_with(AT_SEA, mode=1), "mode", INVALID_FORMAT),
        (_with(AT_SEA, items={"lineNo": 1}), "items", INVALID_FORMAT),
    ],
)
def test_rejects_record_with_field_problem(record, field_name, error_code):
    result = parse_tms_shipments([RECEIVED, record])

    assert [s.shipment_code for s in result.shipments] == ["SHP-NORMAL-001"]
    [rejection] = result.rejections
    assert (rejection.field_name, rejection.error_code) == (field_name, error_code)


@pytest.mark.parametrize(
    ("line", "field_name", "error_code"),
    [
        ({"sku": "TMS-SKU-003", "qty": 60}, "items[1].lineNo", MISSING_REQUIRED),
        ({"lineNo": 2, "qty": 60}, "items[1].sku", MISSING_REQUIRED),
        ({"lineNo": 2, "sku": "TMS-SKU-003"}, "items[1].qty", MISSING_REQUIRED),
        (
            {"lineNo": 2, "sku": "TMS-SKU-003", "qty": "60"},
            "items[1].qty",
            INVALID_FORMAT,
        ),
        (
            {"lineNo": 2, "sku": "TMS-SKU-003", "qty": 60.0},
            "items[1].qty",
            INVALID_FORMAT,
        ),
        (
            {"lineNo": True, "sku": "TMS-SKU-003", "qty": 60},
            "items[1].lineNo",
            INVALID_FORMAT,
        ),
        ("TMS-SKU-003", "items[1]", INVALID_FORMAT),
    ],
)
def test_one_bad_line_rejects_whole_shipment(line, field_name, error_code):
    """계약 4: 상세 줄 하나가 원인이어도 부모 선적 1건을 거부하고, 키는 선적 코드, 위치는 items[i].필드"""
    record = _with(AT_SEA, items=[AT_SEA["items"][0], line])

    result = parse_tms_shipments([record])

    assert result.shipments == []
    assert result.rejections == [
        Rejection(
            source_record_key="SHP-DELAY-002",
            field_name=field_name,
            error_code=error_code,
            error_message=result.rejections[0].error_message,
        )
    ]


def test_record_without_usable_code_is_keyed_by_position():
    result = parse_tms_shipments([RECEIVED, ["not", "an", "object"], {"mode": "AIR"}])

    assert [(r.source_record_key, r.error_code) for r in result.rejections] == [
        ("data[1]", INVALID_FORMAT),
        ("data[2]", MISSING_REQUIRED),
    ]


def test_empty_response_is_empty():
    assert parse_tms_shipments([]) == parse_tms_shipments(())
    assert parse_tms_shipments([]).shipments == []
