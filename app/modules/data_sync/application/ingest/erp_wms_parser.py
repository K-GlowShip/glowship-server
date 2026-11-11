from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.modules.data_sync.application.ingest.code_resolver import (
    MISSING_REQUIRED,
    Rejection,
)
from app.modules.data_sync.application.ingest.source_payload import (
    SourceInventory,
    SourceOrder,
    SourceOrderLine,
)
from app.modules.data_sync.application.ingest.tms_parser import (
    INVALID_DATE,
    INVALID_FORMAT,
    _date,
    _integer,
    _Reject,
    _required,
    _text,
)

"""
    가상 ERP(주문)·WMS(재고) 응답 파서 (GLOW-13, 2026-11-09 결정 "ERP·WMS 도 동기화")
      - 가상 원천 시나리오(virtual_sources)의 data[] 한 건(JSON 객체)을 SourceOrder·SourceInventory 로 바꿉니다.
        필드 이름은 수집 레코드와 같은 snake_case 입니다(census tools/synth2026/extend_days.py 가 만드는 모양):
          주문: order_code·sales_channel·customer_region·warehouse_code·ordered_at·promised_delivery_at·status·
                lines[].line_no·sku_code·ordered_quantity·shipped_quantity·line_status   (ERP 는 내부 코드)
          재고: inventory_date·warehouse_code·sku_code·on_hand_quantity·reserved_quantity·available_quantity·
                outbound_quantity   (WMS 매핑 코드)
      - 파서 계약은 TMS 파서와 같습니다(source_payload 1~4): 타입, 필수값, 날짜·시각 형식, 레코드 단위 거부.
        일시는 시간대가 있는 ISO 8601 이어야 하고, 아니면 INVALID_DATE. 값의 의미는 적재 서비스가 검사합니다.
      - 거부 키는 적재 서비스와 같은 모양입니다: 주문은 order_code, 재고는 창고|SKU|일자. 읽을 수 없으면 data[i].
"""


@dataclass(frozen=True)
class ParseResult:
    records: list
    rejections: list[Rejection]


def parse_erp_orders(data: Sequence[Any]) -> ParseResult:
    return _parse(data, _order, _order_key)


def parse_wms_inventory(data: Sequence[Any]) -> ParseResult:
    return _parse(data, _inventory, _inventory_key)


def _parse(
    data: Sequence[Any],
    build: Callable[[Mapping], Any],
    key: Callable[[Any, int], str],
) -> ParseResult:
    records, rejections = [], []
    for index, item in enumerate(data):
        try:
            if not isinstance(item, Mapping):
                raise _Reject("", INVALID_FORMAT, "레코드가 JSON 객체가 아님")
            records.append(build(item))
        except _Reject as reject:
            rejections.append(
                Rejection(
                    source_record_key=key(item, index),
                    field_name=reject.field_name,
                    error_code=reject.error_code,
                    error_message=reject.error_message,
                )
            )
    return ParseResult(records=records, rejections=rejections)


def _order_key(item: Any, index: int) -> str:
    if isinstance(item, Mapping) and isinstance(item.get("order_code"), str):
        return item["order_code"]
    return f"data[{index}]"


def _inventory_key(item: Any, index: int) -> str:
    if isinstance(item, Mapping):
        parts = [item.get(k) for k in ("warehouse_code", "sku_code", "inventory_date")]
        if all(isinstance(p, str) and p for p in parts):
            return "|".join(parts)
    return f"data[{index}]"


def _timestamp(record: Mapping, name: str) -> datetime:
    value = record.get(name)
    if value is None:
        raise _Reject(name, MISSING_REQUIRED, f"필수 값 {name} 없음")
    parsed = None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise _Reject(
            name,
            INVALID_DATE,
            f"{name} 을 시간대 있는 ISO 8601 시각으로 읽을 수 없음 ({value!r})",
        )
    return parsed


def _order(item: Mapping) -> SourceOrder:
    lines = _required(item, "lines", list)
    return SourceOrder(
        order_code=_text(item, "order_code"),
        sales_channel=_text(item, "sales_channel"),
        customer_region=_text(item, "customer_region"),
        warehouse_code=_text(item, "warehouse_code"),
        ordered_at=_timestamp(item, "ordered_at"),
        promised_delivery_at=_timestamp(item, "promised_delivery_at"),
        status=_text(item, "status"),
        lines=tuple(_order_line(line, index) for index, line in enumerate(lines)),
    )


def _order_line(line: Any, index: int) -> SourceOrderLine:
    where = f"lines[{index}]"
    if not isinstance(line, Mapping):
        raise _Reject(where, INVALID_FORMAT, "주문 상세가 JSON 객체가 아님")
    return SourceOrderLine(
        line_no=_integer(line, "line_no", where),
        sku_code=_text(line, "sku_code", where),
        ordered_quantity=_integer(line, "ordered_quantity", where),
        shipped_quantity=_integer(line, "shipped_quantity", where),
        line_status=_text(line, "line_status", where),
    )


def _inventory(item: Mapping) -> SourceInventory:
    return SourceInventory(
        inventory_date=_date(item, "inventory_date", required=True),
        warehouse_code=_text(item, "warehouse_code"),
        sku_code=_text(item, "sku_code"),
        on_hand_quantity=_integer(item, "on_hand_quantity"),
        reserved_quantity=_integer(item, "reserved_quantity"),
        available_quantity=_integer(item, "available_quantity"),
        outbound_quantity=_integer(item, "outbound_quantity"),
    )
