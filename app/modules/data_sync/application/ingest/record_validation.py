from collections.abc import Iterable

from app.modules.data_sync.application.ingest.code_resolver import Rejection
from app.modules.data_sync.application.ingest.source_payload import (
    SourceInventory,
    SourceOrder,
    SourceShipment,
    SourceSku,
    SourceWarehouse,
)

"""
    적용 전 레코드 의미 검증 (GLOW-13)
      - 값이 DB CHECK 제약을 어기는 레코드는 적용 전에 거부합니다. 그대로 적용하면 IntegrityError 1건으로
        수집 전체가 롤백되어 FAILED 가 됩니다 (확정 기준선 B사 주문 수집은 레코드 단위로 거부하고 PARTIAL_SUCCESS).
      - 대상 제약 (v6.2 DDL): skus_ck1~3, warehouses_ck1~2, orders_ck1~3, order_lines_ck1~3,
        shipments_ck2~3, shipment_lines_ck1, inventory_daily_ck1~7.
        shipments_ck1(운송 수단)은 code_resolver 의 UNSUPPORTED_TRANSPORT_MODE 가 맡습니다.
      - 타입·필수값·시간대는 파서 계약(source_payload)이라 여기서 다시 보지 않습니다.
      - 레코드에서 처음 어긴 값 하나를 사유로 돌려줍니다 (레코드당 거부 1건). 상세 줄이 원인이면 키는 부모 코드,
        위치는 field_name(lines[i].필드)입니다 (code_resolver 거부와 같은 형식).
      - DB 를 읽지 않는 순수 함수입니다.
"""

# 오류 코드. NEGATIVE_QUANTITY 는 확정 기준선 data_validation_issues.csv 와 같은 이름입니다.
NEGATIVE_QUANTITY = "NEGATIVE_QUANTITY"
INVALID_QUANTITY = "INVALID_QUANTITY"  # 0 보다 커야 하는 수량이 0
INVALID_VALUE = "INVALID_VALUE"  # 허용 목록에 없는 값
INCONSISTENT_VALUE = "INCONSISTENT_VALUE"  # 필드 사이 관계가 어긋남

# 허용 값 (v6.2 DDL CHECK 와 같음. test_record_validation 이 ORM CHECK 와 비교합니다)
POPULARITY_TIERS = frozenset({"HIT", "REGULAR", "LONG_TAIL"})  # skus_ck1
SKU_STATUSES = frozenset({"ACTIVE", "INACTIVE"})  # skus_ck3
WAREHOUSE_TYPES = frozenset({"KR_ORIGIN", "US_3PL"})  # warehouses_ck1
REGION_CODES = frozenset({"KR", "WEST", "EAST"})  # warehouses_ck2
CUSTOMER_REGIONS = frozenset({"WEST", "EAST"})  # orders_ck1
SALES_CHANNELS = frozenset({"AMAZON", "SEPHORA"})  # orders_ck2
ORDER_STATUSES = frozenset(
    {
        "OPEN",
        "PARTIALLY_SHIPPED",
        "SHIPPED",
        "DELIVERED",
        "CANCELLED",
        "REMOVED_AT_SOURCE",
    }
)  # orders_ck3
LINE_STATUSES = frozenset({"ACTIVE", "REMOVED_AT_SOURCE"})  # order_lines_ck3
SHIPMENT_STAGES = frozenset(
    {"PRE_DEPARTURE", "AT_SEA", "IN_AIR", "AT_PORT", "RECEIVED"}
)  # shipments_ck2
RECEIVED = "RECEIVED"


def validate_sku(source: SourceSku) -> Rejection | None:
    key = source.sku_code
    return _first(
        _allowed(key, "popularity_tier", source.popularity_tier, POPULARITY_TIERS),
        _quantity(key, "units_per_case", source.units_per_case, positive=True),
        _allowed(key, "status", source.status, SKU_STATUSES),
    )


def validate_warehouse(source: SourceWarehouse) -> Rejection | None:
    key = source.warehouse_code
    return _first(
        _allowed(key, "warehouse_type", source.warehouse_type, WAREHOUSE_TYPES),
        _allowed(key, "region_code", source.region_code, REGION_CODES),
    )


def validate_order(source: SourceOrder) -> Rejection | None:
    key = source.order_code
    checks = [
        _allowed(key, "customer_region", source.customer_region, CUSTOMER_REGIONS),
        _allowed(key, "sales_channel", source.sales_channel, SALES_CHANNELS),
        _allowed(key, "status", source.status, ORDER_STATUSES),
    ]
    for index, line in enumerate(source.lines):
        at = f"lines[{index}]"
        checks += [
            _quantity(
                key, f"{at}.ordered_quantity", line.ordered_quantity, positive=True
            ),
            _quantity(key, f"{at}.shipped_quantity", line.shipped_quantity),
            _inconsistent(
                key,
                f"{at}.shipped_quantity",
                line.shipped_quantity > line.ordered_quantity,
                f"출고 수량 {line.shipped_quantity} 이 주문 수량 {line.ordered_quantity} 보다 큼",
            ),
            _allowed(key, f"{at}.line_status", line.line_status, LINE_STATUSES),
        ]
    return _first(*checks)


def validate_shipment(source: SourceShipment) -> Rejection | None:
    key = source.shipment_code
    received = source.current_stage == RECEIVED
    checks = [
        _allowed(key, "current_stage", source.current_stage, SHIPMENT_STAGES),
        _inconsistent(
            key,
            "actual_3pl_inbound_date",
            received != (source.actual_3pl_inbound_date is not None),
            "입고 완료(RECEIVED)와 실제 3PL 입고일은 함께 있어야 함"
            f" (단계 {source.current_stage}, 실제 입고일 {source.actual_3pl_inbound_date})",
        ),
    ]
    for index, line in enumerate(source.lines):
        checks.append(
            _quantity(
                key,
                f"lines[{index}].shipped_quantity",
                line.shipped_quantity,
                positive=True,
            )
        )
    return _first(*checks)


def validate_inventory(source: SourceInventory, key: str) -> Rejection | None:
    """key 는 재고 거부 키(code_resolver.inventory_record_key)입니다."""
    on_hand, reserved, available = (
        source.on_hand_quantity,
        source.reserved_quantity,
        source.available_quantity,
    )
    return _first(
        _quantity(key, "on_hand_quantity", on_hand),
        _quantity(key, "reserved_quantity", reserved),
        _quantity(key, "available_quantity", available),
        _quantity(key, "outbound_quantity", source.outbound_quantity),
        # 음수가 아니면 ck5(보유 = 예약 + 가용)가 ck6·ck7(예약·가용 ≤ 보유)도 보장합니다
        _inconsistent(
            key,
            "on_hand_quantity",
            on_hand != reserved + available,
            f"보유 {on_hand} ≠ 예약 {reserved} + 가용 {available}",
        ),
    )


def _first(*checks: Rejection | None) -> Rejection | None:
    return next((check for check in checks if check is not None), None)


def _allowed(
    key: str, field_name: str, value: str, allowed: Iterable[str]
) -> Rejection | None:
    if value in allowed:
        return None
    return Rejection(
        key,
        field_name,
        INVALID_VALUE,
        f"허용하지 않는 값: {value} ({'·'.join(sorted(allowed))})",
    )


def _quantity(
    key: str, field_name: str, value: int, *, positive: bool = False
) -> Rejection | None:
    if value < 0:
        return Rejection(key, field_name, NEGATIVE_QUANTITY, f"음수 수량: {value}")
    if positive and value == 0:
        return Rejection(key, field_name, INVALID_QUANTITY, "0 보다 커야 하는 수량이 0")
    return None


def _inconsistent(
    key: str, field_name: str, broken: bool, message: str
) -> Rejection | None:
    return Rejection(key, field_name, INCONSISTENT_VALUE, message) if broken else None
