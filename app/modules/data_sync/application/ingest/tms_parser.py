import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.modules.data_sync.application.ingest.code_resolver import (
    MISSING_REQUIRED,
    Rejection,
)
from app.modules.data_sync.application.ingest.source_payload import (
    SourceShipment,
    SourceShipmentLine,
)

"""
    TMS 선적 응답 파서 (GLOW-13·49, source_payload 의 파서 계약)
      - TMS 응답 data[] 의 선적 1건(JSON 객체)을 SourceShipment 로 바꿉니다. 가상 TMS·수동 수집(API 25)이 씁니다.
      - 필드는 TMS 응답 그대로입니다 (census_kbeauty source_samples/shipments_api.json, erd_meeting_guide 6절):
        shipmentNo·mode·destWh·plannedKrRelease·stage·etaPort·eta3pl·actual3pl·items[].lineNo·sku·qty.
        미국 항구·시장 리드타임 기준·계획 3PL 입고일은 TMS 가 보내지 않고 서버가 정합니다 (code_resolver).
      - '_demo' 로 시작하는 필드는 데모 검증용 메타데이터라 읽지 않습니다. 모르는 필드도 무시합니다.
      - 파서가 보장하는 것(계약 1~4): 타입, 필수값, 날짜 형식, 레코드 단위 거부. 거부 키는 shipmentNo,
        없으면 응답 안 위치(data[i]), 위치는 TMS 필드 이름(items[i].qty 등)입니다.
      - 값의 의미(단계 값·운송 수단·수량 양수·입고 단계와 실제 입고일 관계)는 적재 서비스가 검사합니다 (record_validation).
"""

# 오류 코드 이름은 확정 기준선(data_validation_issues.csv)과 같게 씁니다.
INVALID_DATE = "INVALID_DATE"
# 기준선에 없는 이름: 값은 있으나 타입이 다름 (예: 수량이 문자열). 계약 1(타입)을 지키려고 레코드를 거부합니다.
INVALID_FORMAT = "INVALID_FORMAT"

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class TmsParseResult:
    shipments: list[SourceShipment]
    rejections: list[Rejection]


class _Reject(Exception):
    def __init__(self, field_name: str, error_code: str, error_message: str):
        self.field_name = field_name
        self.error_code = error_code
        self.error_message = error_message


def parse_tms_shipments(data: Sequence[Any]) -> TmsParseResult:
    """TMS 응답 data[] 를 선적 레코드와 거부로 나눕니다. 순서는 응답 순서 그대로입니다."""
    shipments: list[SourceShipment] = []
    rejections: list[Rejection] = []
    for index, item in enumerate(data):
        key = _record_key(item, index)
        try:
            shipments.append(_shipment(item))
        except _Reject as reject:
            rejections.append(
                Rejection(
                    source_record_key=key,
                    field_name=reject.field_name,
                    error_code=reject.error_code,
                    error_message=reject.error_message,
                )
            )
    return TmsParseResult(shipments=shipments, rejections=rejections)


def _record_key(item: Any, index: int) -> str:
    if isinstance(item, Mapping):
        code = item.get("shipmentNo")
        if isinstance(code, str) and code.strip():
            return code
    return f"data[{index}]"


def _shipment(item: Any) -> SourceShipment:
    if not isinstance(item, Mapping):
        raise _Reject("", INVALID_FORMAT, "선적 레코드가 JSON 객체가 아님")
    items = _required(item, "items", list)
    return SourceShipment(
        shipment_code=_text(item, "shipmentNo"),
        transport_mode=_text(item, "mode"),
        warehouse_code=_text(item, "destWh"),
        planned_kr_release_date=_date(item, "plannedKrRelease", required=True),
        current_stage=_text(item, "stage"),
        current_expected_port_arrival_date=_date(item, "etaPort", required=False),
        current_expected_3pl_inbound_date=_date(item, "eta3pl", required=True),
        actual_3pl_inbound_date=_date(item, "actual3pl", required=False),
        lines=tuple(_line(line, index) for index, line in enumerate(items)),
    )


def _line(line: Any, index: int) -> SourceShipmentLine:
    where = f"items[{index}]"
    if not isinstance(line, Mapping):
        raise _Reject(where, INVALID_FORMAT, "선적 상세가 JSON 객체가 아님")
    return SourceShipmentLine(
        line_no=_integer(line, "lineNo", where),
        sku_code=_text(line, "sku", where),
        shipped_quantity=_integer(line, "qty", where),
    )


def _field(where: str, name: str) -> str:
    return f"{where}.{name}" if where else name


def _required(record: Mapping, name: str, kind: type, where: str = "") -> Any:
    value = record.get(name)
    field_name = _field(where, name)
    if value is None:
        raise _Reject(field_name, MISSING_REQUIRED, f"필수 값 {field_name} 없음")
    if not isinstance(value, kind) or isinstance(value, bool) and kind is not bool:
        raise _Reject(
            field_name,
            INVALID_FORMAT,
            f"{field_name} 의 타입이 {kind.__name__} 가 아님 ({value!r})",
        )
    return value


def _text(record: Mapping, name: str, where: str = "") -> str:
    value = _required(record, name, str, where)
    if not value.strip():
        field_name = _field(where, name)
        raise _Reject(
            field_name, MISSING_REQUIRED, f"필수 값 {field_name} 이 비어 있음"
        )
    return value


def _integer(record: Mapping, name: str, where: str = "") -> int:
    return _required(record, name, int, where)


def _date(record: Mapping, name: str, *, required: bool) -> date | None:
    value = record.get(name)
    if value is None:
        if required:
            raise _Reject(name, MISSING_REQUIRED, f"필수 값 {name} 없음")
        return None
    # 문자열이 아닌 값(숫자 20260702 등)도 날짜로 읽을 수 없는 값이라 INVALID_DATE (계약 3)
    if isinstance(value, str) and _DATE.fullmatch(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise _Reject(
        name, INVALID_DATE, f"{name} 을 YYYY-MM-DD 날짜로 읽을 수 없음 ({value!r})"
    )
