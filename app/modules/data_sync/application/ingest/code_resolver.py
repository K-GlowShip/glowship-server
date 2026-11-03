import math
import uuid
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.modules.data_sync.application.ingest.source_payload import (
    SourceInventory,
    SourceOrder,
    SourceShipment,
)
from app.modules.data_sync.application.ingest.source_records import (
    InventoryRecord,
    OrderLineRecord,
    OrderRecord,
    ShipmentLineRecord,
    ShipmentRecord,
)

"""
    외부 코드 → 내부 ID 변환 (GLOW-13)
      - 원천 레코드의 SKU·창고 코드를 고객사 코드표(CodeBook)로 내부 ID 로 바꿉니다.
        바꿀 수 없는 레코드는 통째로 거부하고 이유를 돌려줍니다. 거부 건수는 수집 실행의 rejected_record_count 가 되고,
        검증 오류 기록(data_validation_issues)은 GLOW-15 가 이 거부 사유로 남깁니다.
      - 선적의 미국 항구·시장 리드타임 기준·계획 3PL 입고일은 원천(TMS)이 보내지 않고 서버가 정합니다.
          항구: (도착 창고 지역, 운송 수단) 경로표 ROUTE_PORTS
          HS10: 선적 상세 SKU 의 HS10. 정확히 하나여야 합니다 (기준 고유 키 market_leadtime_references_uk1 이
                (월, 항구, HS10, 운송 수단)이고 선적은 기준 하나만 가리킴). 둘 이상이면 MIXED_HS10_IN_SHIPMENT,
                상세가 없어 정할 수 없으면 MISSING_REQUIRED 로 거부합니다.
          기준: 같은 (항구, HS10, 운송 수단)의 시장 기준을 아래 순서로 찾고, 끝내 없으면 거부합니다.
                마지막으로 인정한 기준 월(LAST_ACCEPTED_REFERENCE_MONTH) 뒤의 행은 쓰지 않습니다 (합성데이터 결정 H-4).
                1) 출고 월 기준  2) 인정 범위 안 가장 최근 연도의 같은 달  3) 같은 경로의 가장 가까운 이전 달
          계획 3PL 입고일 = 한국 계획 출고일 + 올림(p90)
        기준과 계획일은 선적이 처음 들어올 때만 정하고, 이미 있는 선적은 저장된 값을 그대로 씁니다.
        근거: serviceflow v6.2 1절 "계획 입고일"·7절 결정표 "선적 계획 기준"(결정). 결정 원문은 docs/data-sync/ingest.md 9절.
        그래서 HS10 검사·상세 필수도 새 선적에만 합니다. 이미 있는 선적은 상세 없이 머리(단계·예상일)만 와도 되고,
        응답에서 빠진 기존 상세는 그대로 남습니다 (change_detection.diff_shipment).
      - DB 를 읽지 않는 순수 함수입니다. 코드표는 Repository 가 만들어 넘깁니다.
"""

# 시장 마스터의 이 달 뒤 행은 저신뢰 추정(imputed) 행이라 쓰지 않습니다 (합성데이터 결정 H-4, ingest.md 9절)
LAST_ACCEPTED_REFERENCE_MONTH = "2026-01"

# 선적 운송 수단 → 시장 리드타임 기준의 운송 수단. 이 밖의 값(예: SEA)은 거부합니다.
TRANSPORT_MODE_TO_REFERENCE = {"OCEAN": "vessel_container", "AIR": "air"}

# (도착 창고 지역, 운송 수단) → 미국 항구 코드 (생성기 경로표 ROUTES, 2026 상반기 선적 166건과 일치)
ROUTE_PORTS = {
    ("WEST", "OCEAN"): "2704",  # LOS ANGELES
    ("EAST", "AIR"): "1003",  # NEWARK
    ("WEST", "AIR"): "2720",  # LOS ANGELES 항공
}

# 오류 코드 이름은 확정 기준선(data_validation_issues.csv)과 같게 씁니다.
UNKNOWN_SKU = "UNKNOWN_SKU"
UNKNOWN_WAREHOUSE = "UNKNOWN_WAREHOUSE"
UNSUPPORTED_TRANSPORT_MODE = "UNSUPPORTED_TRANSPORT_MODE"
NO_ROUTE_PORT = "NO_ROUTE_PORT"
NO_LEADTIME_REFERENCE = "NO_LEADTIME_REFERENCE"
# 주문·선적 안에 같은 상세 줄 번호가 2개 이상: 어느 줄이 맞는지 알 수 없어 부모 레코드 전체를 1건으로 거부합니다
# (그대로 두면 (부모, line_no) 고유 제약 order_lines_uk2·shipment_lines_uk2 위반으로 수집 전체가 실패).
DUPLICATE_LINE_NO = "DUPLICATE_LINE_NO"
# 새 선적의 상세가 없음: HS10·리드타임 기준을 정할 수 없음 (기준선과 같은 이름)
MISSING_REQUIRED = "MISSING_REQUIRED"
# 새 선적 상세 SKU 의 HS10 이 둘 이상: 기준을 하나로 정할 수 없음 (임의로 고르지 않음)
MIXED_HS10_IN_SHIPMENT = "MIXED_HS10_IN_SHIPMENT"


@dataclass(frozen=True)
class WarehouseCode:
    warehouse_id: uuid.UUID
    warehouse_code: str  # 내부 창고 코드 (변경 요약 문장용)
    region_code: str  # 항구 경로표 키


@dataclass(frozen=True)
class SkuCode:
    sku_id: uuid.UUID
    sku_code: str  # 내부 SKU 코드 (변경 요약 문장용)
    hs10: str  # 새 선적의 시장 리드타임 기준 찾기용


@dataclass(frozen=True)
class LeadtimeReference:
    market_leadtime_reference_id: uuid.UUID
    total_leadtime_p90: float


@dataclass(frozen=True)
class CodeBook:
    """한 고객사·원천 유형의 외부 코드표. 키는 원천이 보내는 코드입니다."""

    skus: Mapping[str, SkuCode]
    warehouses: Mapping[str, WarehouseCode]
    # (YYYY-MM, us_port_code, hs10, 기준 운송 수단) → 기준 (market_leadtime_references_uk1). 선적 원천에서만 씁니다.
    leadtime_references: Mapping[tuple[str, str, str, str], LeadtimeReference] = field(
        default_factory=dict
    )
    last_accepted_reference_month: str = LAST_ACCEPTED_REFERENCE_MONTH


@dataclass(frozen=True)
class ShipmentPlan:
    """이미 있는 선적의 저장된 계획 기준 (다시 계산하지 않음)"""

    market_leadtime_reference_id: uuid.UUID
    planned_3pl_inbound_date: date


@dataclass(frozen=True)
class Rejection:
    """거부한 원천 레코드 1건 (GLOW-15 가 data_validation_issues 로 남길 정보)"""

    source_record_key: str
    field_name: str
    error_code: str
    error_message: str


def resolve_order(source: SourceOrder, book: CodeBook) -> OrderRecord | Rejection:
    if duplicate := _duplicate_line_no(source.order_code, source.lines):
        return duplicate
    warehouse = book.warehouses.get(source.warehouse_code)
    if warehouse is None:
        return _unknown_warehouse(source.order_code, source.warehouse_code)
    lines = []
    for index, line in enumerate(source.lines):
        sku = book.skus.get(line.sku_code)
        if sku is None:
            return _unknown_sku(
                source.order_code, f"lines[{index}].sku_code", line.sku_code
            )
        lines.append(
            OrderLineRecord(
                line_no=line.line_no,
                sku_id=sku.sku_id,
                ordered_quantity=line.ordered_quantity,
                shipped_quantity=line.shipped_quantity,
                line_status=line.line_status,
            )
        )
    return OrderRecord(
        order_code=source.order_code,
        sales_channel=source.sales_channel,
        customer_region=source.customer_region,
        warehouse_id=warehouse.warehouse_id,
        ordered_at=source.ordered_at,
        promised_delivery_at=source.promised_delivery_at,
        status=source.status,
        lines=tuple(lines),
    )


def resolve_shipment(
    source: SourceShipment, book: CodeBook, existing_plan: ShipmentPlan | None = None
) -> ShipmentRecord | Rejection:
    """existing_plan 이 있으면(이미 있는 선적) 저장된 기준·계획일을 그대로 쓰고, 없으면 새로 정합니다."""
    key = source.shipment_code
    if duplicate := _duplicate_line_no(key, source.lines):
        return duplicate
    reference_mode = TRANSPORT_MODE_TO_REFERENCE.get(source.transport_mode)
    if reference_mode is None:
        return Rejection(
            key,
            "transport_mode",
            UNSUPPORTED_TRANSPORT_MODE,
            f"지원하지 않는 운송 수단: {source.transport_mode} (OCEAN·AIR)",
        )
    warehouse = book.warehouses.get(source.warehouse_code)
    if warehouse is None:
        return _unknown_warehouse(key, source.warehouse_code)
    lines, hs10s = [], set()
    for index, line in enumerate(source.lines):
        sku = book.skus.get(line.sku_code)
        if sku is None:
            return _unknown_sku(key, f"lines[{index}].sku_code", line.sku_code)
        lines.append(
            ShipmentLineRecord(line.line_no, sku.sku_id, line.shipped_quantity)
        )
        hs10s.add(sku.hs10)

    if existing_plan is None:
        if not hs10s:
            return Rejection(
                key,
                "lines",
                MISSING_REQUIRED,
                "새 선적에 상세가 없어 HS10·시장 리드타임 기준을 정할 수 없음",
            )
        if len(hs10s) > 1:
            return Rejection(
                key,
                "lines",
                MIXED_HS10_IN_SHIPMENT,
                f"새 선적 상세의 HS10 이 둘 이상: {', '.join(sorted(hs10s))}",
            )
        [hs10] = hs10s
        port = ROUTE_PORTS.get((warehouse.region_code, source.transport_mode))
        if port is None:
            return Rejection(
                key,
                "warehouse_code",
                NO_ROUTE_PORT,
                f"경로표에 없는 경로: {warehouse.region_code} {source.transport_mode}",
            )
        reference = find_leadtime_reference(
            book.leadtime_references,
            source.planned_kr_release_date,
            port,
            hs10,
            reference_mode,
            book.last_accepted_reference_month,
        )
        if reference is None:
            return Rejection(
                key,
                "planned_kr_release_date",
                NO_LEADTIME_REFERENCE,
                f"시장 리드타임 기준 없음: {source.planned_kr_release_date:%Y-%m}"
                f" {port} HS10 {hs10} {source.transport_mode}",
            )
        existing_plan = ShipmentPlan(
            reference.market_leadtime_reference_id,
            planned_3pl_inbound_date(
                source.planned_kr_release_date, reference.total_leadtime_p90
            ),
        )

    return ShipmentRecord(
        shipment_code=source.shipment_code,
        transport_mode=source.transport_mode,
        warehouse_id=warehouse.warehouse_id,
        market_leadtime_reference_id=existing_plan.market_leadtime_reference_id,
        planned_kr_release_date=source.planned_kr_release_date,
        planned_3pl_inbound_date=existing_plan.planned_3pl_inbound_date,
        current_stage=source.current_stage,
        current_expected_port_arrival_date=source.current_expected_port_arrival_date,
        current_expected_3pl_inbound_date=source.current_expected_3pl_inbound_date,
        actual_3pl_inbound_date=source.actual_3pl_inbound_date,
        lines=tuple(lines),
    )


def resolve_inventory(
    source: SourceInventory, book: CodeBook
) -> InventoryRecord | Rejection:
    key = inventory_record_key(source, book)
    warehouse = book.warehouses.get(source.warehouse_code)
    if warehouse is None:
        return _unknown_warehouse(key, source.warehouse_code)
    sku = book.skus.get(source.sku_code)
    if sku is None:
        return _unknown_sku(key, "sku_code", source.sku_code)
    return InventoryRecord(
        inventory_date=source.inventory_date,
        warehouse_id=warehouse.warehouse_id,
        sku_id=sku.sku_id,
        on_hand_quantity=source.on_hand_quantity,
        reserved_quantity=source.reserved_quantity,
        available_quantity=source.available_quantity,
        outbound_quantity=source.outbound_quantity,
        warehouse_code=warehouse.warehouse_code,
        sku_code=sku.sku_code,
    )


def inventory_record_key(source: SourceInventory, book: CodeBook) -> str:
    """
    재고 거부 키 `창고|SKU|일자` (확정 기준선 data_validation_issues.csv 와 같은 형식).
      - 창고 자리는 매핑 여부와 관계없이 원천의 외부 창고 코드입니다
        (기준선: 매핑 없는 WMS-ATL01, 매핑된 WMS-EWR01 모두 외부 코드. 검증 엔진 snapshot.py 는 이 자리를 외부 코드로 조회).
      - SKU 자리는 매핑되면 내부 sku_code, 매핑이 없으면 원천의 외부 SKU 코드입니다
        (상품 탭 GET /skus 가 split_part(key, '|', 2) 를 내부 sku_code 로 읽음).
    """
    sku = book.skus.get(source.sku_code)
    sku_part = sku.sku_code if sku else source.sku_code
    return f"{source.warehouse_code}|{sku_part}|{source.inventory_date.isoformat()}"


def find_leadtime_reference(
    references: Mapping[tuple[str, str, str, str], LeadtimeReference],
    planned_kr_release_date: date,
    us_port_code: str,
    hs10: str,
    reference_mode: str,
    last_accepted_month: str = LAST_ACCEPTED_REFERENCE_MONTH,
) -> LeadtimeReference | None:
    """
    같은 (항구, HS10, 운송 수단) 안에서: 출고 월 기준 → 인정 범위 안 가장 최근 연도의 같은 달 → 가장 가까운 이전 달.
    인정 범위는 last_accepted_month 이하이고 출고 월 이하인 기준입니다.
    """
    release_month = planned_kr_release_date.strftime("%Y-%m")
    usable = {
        month: reference
        for (month, port, code, mode), reference in references.items()
        if port == us_port_code
        and code == hs10
        and mode == reference_mode
        and month <= last_accepted_month
        and month <= release_month
    }
    if release_month in usable:
        return usable[release_month]
    same_month = [m for m in usable if m[5:7] == release_month[5:7]]
    if same_month:
        return usable[max(same_month)]
    if usable:
        return usable[max(usable)]
    return None


def planned_3pl_inbound_date(
    planned_kr_release_date: date, total_leadtime_p90: float
) -> date:
    """계획 3PL 입고일 = 한국 계획 출고일 + 올림(시장 리드타임 p90) 일"""
    return planned_kr_release_date + timedelta(days=math.ceil(total_leadtime_p90))


def _duplicate_line_no(key: str, lines) -> Rejection | None:
    """상세 줄 번호가 겹치면 겹친 번호 개수와 관계없이 부모 레코드에 대한 거부 1건"""
    counts = Counter(line.line_no for line in lines)
    duplicated = sorted(line_no for line_no, count in counts.items() if count > 1)
    if not duplicated:
        return None
    return Rejection(
        key,
        "line_no",
        DUPLICATE_LINE_NO,
        f"같은 상세 줄 번호가 2개 이상: {', '.join(map(str, duplicated))}",
    )


def _unknown_sku(key: str, field_name: str, code: str) -> Rejection:
    """상세 줄의 SKU 이면 키는 부모 코드이고 위치는 field_name(lines[i].sku_code)에 둡니다 (기준선과 같음)."""
    return Rejection(key, field_name, UNKNOWN_SKU, f"매핑되지 않은 SKU 코드: {code}")


def _unknown_warehouse(key: str, code: str) -> Rejection:
    return Rejection(
        key,
        "warehouse_code",
        UNKNOWN_WAREHOUSE,
        f"매핑되지 않은 창고 코드: {code}",
    )
