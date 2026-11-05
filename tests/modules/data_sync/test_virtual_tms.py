import json
from datetime import UTC, datetime

import pytest

from app.modules.data_sync.application.ingest.tms_parser import parse_tms_shipments
from app.modules.data_sync.infrastructure.virtual_tms import (
    VirtualTmsScenario,
    VirtualTmsScenarioError,
)

"""
    가상 TMS 시나리오 읽기 (GLOW-49). 아래 선적·날짜는 동작 확인용 시험 데이터이며 데모 시나리오가 아닙니다.
"""


def _at(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 7, day, hour, tzinfo=UTC)


def _shipment(code: str, eta3pl: str, stage: str = "AT_SEA") -> dict:
    return {
        "_demo": {"case": "TEST"},
        "shipmentNo": code,
        "mode": "OCEAN",
        "destWh": "TMS-LAX",
        "plannedKrRelease": "2026-06-20",
        "stage": stage,
        "etaPort": None,
        "eta3pl": eta3pl,
        "actual3pl": None,
        "items": [{"lineNo": 1, "sku": "TMS-SKU-001", "qty": 100}],
    }


DOCUMENT = {
    "interface": "GET /v1/shipments",
    "responses": [
        # 파일 순서가 시각 순서가 아니어도 시각 순서로 읽는다
        {
            "fetched_at": "2026-07-03T00:00:00Z",
            "_demo_company": "A_BEAUTY",
            "data": [_shipment("T-1", "2026-07-25"), _shipment("T-2", "2026-07-28")],
        },
        {
            "fetched_at": "2026-07-02T00:00:00Z",
            "_demo_company": "A_BEAUTY",
            "data": [_shipment("T-1", "2026-07-20")],
        },
        {
            "fetched_at": "2026-07-02T00:00:00Z",
            "_demo_company": "B_COSMETICS",
            "data": [_shipment("B-1", "2026-07-22")],
        },
        {
            "fetched_at": "2026-07-04T09:00:00+09:00",
            "_demo_company": "A_BEAUTY",
            "data": [_shipment("T-3", "2026-08-01")],
        },
    ],
}


@pytest.fixture
def scenario() -> VirtualTmsScenario:
    return VirtualTmsScenario.from_json(DOCUMENT)


def _codes(shipments) -> list[tuple[str, str]]:
    return [(s["shipmentNo"], s["eta3pl"]) for s in shipments]


def test_returns_responses_up_to_until_for_the_company(scenario):
    assert _codes(scenario.shipments_between("A_BEAUTY", None, _at(2))) == [
        ("T-1", "2026-07-20")
    ]
    assert _codes(scenario.shipments_between("B_COSMETICS", None, _at(2))) == [
        ("B-1", "2026-07-22")
    ]
    assert scenario.shipments_between("A_BEAUTY", None, _at(1, 23)) == []
    assert scenario.shipments_between("C_UNKNOWN", None, _at(31)) == []


def test_after_excludes_already_observed_responses(scenario):
    """after < fetched_at ≤ until: 지난 수집 시각까지 받은 응답은 다시 주지 않는다"""
    assert _codes(scenario.shipments_between("A_BEAUTY", _at(2), _at(3))) == [
        ("T-1", "2026-07-25"),
        ("T-2", "2026-07-28"),
    ]
    assert scenario.shipments_between("A_BEAUTY", _at(3), _at(3)) == []


def test_same_shipment_keeps_only_latest_observation(scenario):
    """여러 응답을 한 번에 받으면 같은 선적은 마지막 관측 값 하나 (수집 안 중복 거부를 피함)"""
    shipments = scenario.shipments_between("A_BEAUTY", None, _at(3))

    assert _codes(shipments) == [("T-1", "2026-07-25"), ("T-2", "2026-07-28")]


def test_offset_times_are_compared_as_instants(scenario):
    """07-04 09:00+09:00 = 07-04 00:00Z"""
    assert _codes(scenario.shipments_between("A_BEAUTY", _at(3), _at(4))) == [
        ("T-3", "2026-08-01")
    ]


def test_naive_times_are_refused(scenario):
    with pytest.raises(ValueError):
        scenario.shipments_between("A_BEAUTY", None, datetime(2026, 7, 2))


def test_output_parses_with_tms_parser(scenario):
    parsed = parse_tms_shipments(scenario.shipments_between("A_BEAUTY", None, _at(4)))

    assert parsed.rejections == []
    assert [s.shipment_code for s in parsed.shipments] == ["T-1", "T-2", "T-3"]


def test_record_without_code_is_passed_through_for_parser_to_reject():
    scenario = VirtualTmsScenario.from_json(
        {
            "responses": [
                {
                    "fetched_at": "2026-07-02T00:00:00Z",
                    "_demo_company": "A_BEAUTY",
                    "data": [{"mode": "AIR"}, {"mode": "OCEAN"}],
                }
            ]
        }
    )

    shipments = scenario.shipments_between("A_BEAUTY", None, _at(2))

    assert len(shipments) == 2
    assert len(parse_tms_shipments(shipments).rejections) == 2


def test_from_file_reads_utf8_json(tmp_path):
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(DOCUMENT, ensure_ascii=False), encoding="utf-8")

    scenario = VirtualTmsScenario.from_file(path)

    assert len(scenario.responses) == 4


@pytest.mark.parametrize(
    "document",
    [
        [],
        {"responses": {}},
        {"responses": ["x"]},
        {"responses": [{"fetched_at": "2026-07-02T00:00:00Z", "data": []}]},
        {"responses": [{"_demo_company": "A", "fetched_at": "2026-07-02", "data": []}]},
        {
            "responses": [
                {"_demo_company": "A", "fetched_at": "2026-07-02T00:00:00", "data": []}
            ]
        },
        {"responses": [{"_demo_company": "A", "fetched_at": "soon", "data": []}]},
        {"responses": [{"_demo_company": "A", "fetched_at": "2026-07-02T00:00:00Z"}]},
    ],
)
def test_malformed_scenario_is_a_configuration_error(document):
    with pytest.raises(VirtualTmsScenarioError):
        VirtualTmsScenario.from_json(document)
