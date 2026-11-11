from datetime import UTC, date, datetime

import pytest

from app.modules.data_sync.application.ingest.erp_wms_parser import (
    parse_erp_orders,
    parse_wms_inventory,
)
from app.modules.data_sync.infrastructure.data_sync_enums import SourceDomain
from app.modules.data_sync.infrastructure.virtual_sources import VirtualSourceScenario
from app.modules.data_sync.infrastructure.virtual_tms import VirtualTmsScenarioError

"""
    가상 원천 시나리오(TMS·ERP·WMS)와 ERP·WMS 파서 (GLOW-13, 2026-11-09 결정) — DB 없음
"""


def _order(code="A-ORD-20260702-0001", at="2026-07-02T08:03:00Z", status="OPEN"):
    return {
        "order_code": code,
        "sales_channel": "SEPHORA",
        "customer_region": "WEST",
        "warehouse_code": "WH-WEST",
        "ordered_at": at,
        "promised_delivery_at": "2026-07-09T23:59:59Z",
        "status": status,
        "lines": [
            {
                "line_no": 1,
                "sku_code": "SKU-001",
                "ordered_quantity": 2,
                "shipped_quantity": 0,
                "line_status": "ACTIVE",
            }
        ],
    }


def _inventory(on_hand=10):
    return {
        "inventory_date": "2026-07-02",
        "warehouse_code": "WMS-LAX01",
        "sku_code": "WMS-SKU-001",
        "on_hand_quantity": on_hand,
        "reserved_quantity": 2,
        "available_quantity": on_hand - 2,
        "outbound_quantity": 0,
    }


def _resp(at, domain, data, company="A_BEAUTY"):
    out = {"fetched_at": at, "_demo_company": company, "data": data}
    if domain is not None:
        out["source_domain"] = domain
    return out


SCENARIO = {
    "responses": [
        _resp("2026-07-02T08:03:00Z", "ORDER", [_order()]),
        _resp("2026-07-02T08:12:00Z", "ORDER", [_order("A-ORD-20260702-0002", "2026-07-02T08:12:00Z")]),
        _resp("2026-07-03T00:00:00Z", "ORDER", [_order(status="SHIPPED")]),
        _resp("2026-07-03T00:00:00Z", "INVENTORY", [_inventory(10)]),
        _resp("2026-07-03T00:00:00Z", None, [{"shipmentNo": "OCN-1", "stage": "AT_PORT"}]),
        _resp("2026-07-02T08:05:00Z", "ORDER", [_order()], company="B_COSMETICS"),
    ]
}  # fmt: skip


def _at(text):
    return datetime.fromisoformat(text)


def test_records_between_filters_by_company_domain_and_window():
    scenario = VirtualSourceScenario.from_json(SCENARIO)

    first = scenario.records_between(
        "A_BEAUTY",
        SourceDomain.ORDER,
        _at("2026-07-02T08:00:00Z"),
        _at("2026-07-02T08:10:00Z"),
    )
    second = scenario.records_between(
        "A_BEAUTY",
        SourceDomain.ORDER,
        _at("2026-07-02T08:10:00Z"),
        _at("2026-07-02T08:20:00Z"),
    )

    assert [r["order_code"] for r in first] == ["A-ORD-20260702-0001"]
    assert [r["order_code"] for r in second] == ["A-ORD-20260702-0002"]


def test_same_key_keeps_latest_observation_and_missing_domain_is_shipment():
    scenario = VirtualSourceScenario.from_json(SCENARIO)

    orders = scenario.records_between(
        "A_BEAUTY", SourceDomain.ORDER, None, _at("2026-07-03T00:00:00Z")
    )
    shipments = scenario.records_between(
        "A_BEAUTY", SourceDomain.SHIPMENT, None, _at("2026-07-03T00:00:00Z")
    )

    assert [(r["order_code"], r["status"]) for r in orders] == [
        ("A-ORD-20260702-0002", "OPEN"),
        ("A-ORD-20260702-0001", "SHIPPED"),  # 마지막 관측 값, 마지막 관측 순서
    ]
    assert [s["shipmentNo"] for s in shipments] == ["OCN-1"]
    assert scenario.domains() == {
        SourceDomain.ORDER,
        SourceDomain.INVENTORY,
        SourceDomain.SHIPMENT,
    }
    assert (
        scenario.records_between(
            "A_BEAUTY", SourceDomain.PRODUCT, None, _at("2026-07-03T00:00:00Z")
        )
        == []
    )


def test_from_files_merges_files(tmp_path):
    import json

    a, b = tmp_path / "tms.json", tmp_path / "erp_wms.json"
    a.write_text(
        json.dumps({"responses": SCENARIO["responses"][4:5]}), encoding="utf-8"
    )
    b.write_text(json.dumps({"responses": SCENARIO["responses"][:4]}), encoding="utf-8")

    scenario = VirtualSourceScenario.from_files([a, b])

    assert len(scenario.responses) == 5


@pytest.mark.parametrize(
    "bad",
    [
        {"responses": [_resp("2026-07-02T08:00:00", "ORDER", [])]},  # 시간대 없음
        {"responses": [_resp("2026-07-02T08:00:00Z", "PAYMENT", [])]},  # 원천 아님
        {
            "responses": [{"fetched_at": "2026-07-02T08:00:00Z", "data": []}]
        },  # 고객사 없음
    ],
)
def test_invalid_scenario_is_a_setting_error(bad):
    with pytest.raises(VirtualTmsScenarioError):
        VirtualSourceScenario.from_json(bad)


def test_parse_erp_orders_and_rejections():
    bad_time = _order("A-ORD-X", at="2026-07-02 08:00")
    no_lines = {k: v for k, v in _order("A-ORD-Y").items() if k != "lines"}

    result = parse_erp_orders([_order(), bad_time, no_lines, "x"])

    [order] = result.records
    assert order.ordered_at == datetime(2026, 7, 2, 8, 3, tzinfo=UTC)
    assert order.lines[0].sku_code == "SKU-001"
    assert [
        (r.source_record_key, r.field_name, r.error_code) for r in result.rejections
    ] == [
        ("A-ORD-X", "ordered_at", "INVALID_DATE"),
        ("A-ORD-Y", "lines", "MISSING_REQUIRED"),
        ("data[3]", "", "INVALID_FORMAT"),
    ]


def test_parse_wms_inventory_and_rejections():
    bad = dict(_inventory(), on_hand_quantity="10")

    result = parse_wms_inventory([_inventory(), bad])

    [row] = result.records
    assert (row.inventory_date, row.warehouse_code, row.on_hand_quantity) == (
        date(2026, 7, 2),
        "WMS-LAX01",
        10,
    )
    assert [(r.source_record_key, r.error_code) for r in result.rejections] == [
        ("WMS-LAX01|WMS-SKU-001|2026-07-02", "INVALID_FORMAT")
    ]
