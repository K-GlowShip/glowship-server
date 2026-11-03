import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from app.modules.data_sync.application.ingest.source_records import (
    SCHEDULE_FIELDS,
    InventoryRecord,
    OrderLineRecord,
    OrderRecord,
    ShipmentLineRecord,
    ShipmentRecord,
    SkuRecord,
    WarehouseRecord,
)

"""
    수집 적재 변경 판정 (GLOW-13, serviceflow 5.2 "수집 적재")
      - 원천 레코드를 현재 DB 행과 업무 키로 맞춰 보고, 업무 필드 값이 다른 행만 INSERT·UPDATE 로 돌려줍니다.
        같은 값을 다시 수집하면 아무것도 돌려주지 않습니다 (업무 행·변경 기록을 바꾸지 않음).
      - 변경 기록(data_change_records) 초안을 함께 만듭니다. 영향 SKU 마다 1건이고,
        상세의 SKU 가 바뀌면 변경 전·후 SKU 모두 남깁니다. 창고 변경은 창고로 남깁니다.
      - 변경 요약(change_summary)은 STALE 판정과의 약속입니다 (delay_analysis/application/stale/relevance.py).
        정상 출고는 정확히 "주문 상태 SHIPPED"·"주문 상태 DELIVERED"·"출고 수량" 이어야 관련 변경에서 빠집니다.
      - DB 를 읽거나 쓰지 않는 순수 함수입니다. 새 행의 ID 는 여기서 정합니다 (UUIDv7).
"""

ChangeType = Literal["INSERT", "UPDATE"]

RECEIVED = "RECEIVED"


@dataclass(frozen=True)
class Existing[R]:
    """현재 DB 에 있는 행: 행 ID 와 레코드 모양의 값"""

    row_id: uuid.UUID
    record: R


@dataclass(frozen=True)
class RowChange[R]:
    """적용할 행 하나. INSERT 면 row_id 는 새 ID, changed_fields 는 빈 튜플"""

    change_type: ChangeType
    row_id: uuid.UUID
    record: R
    changed_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChangeRecordDraft:
    """data_change_records 1행 초안 (수집 ID 는 저장할 때 붙입니다)"""

    entity_type: str
    entity_ref: uuid.UUID
    change_type: ChangeType
    sku_id: uuid.UUID | None
    warehouse_id: uuid.UUID | None
    changed_fields: tuple[str, ...] | None
    change_summary: str


@dataclass(frozen=True)
class MasterDiff[R]:
    """상품·창고·재고처럼 상세가 없는 원천의 변경"""

    row: RowChange[R]
    records: tuple[ChangeRecordDraft, ...]


@dataclass(frozen=True)
class OrderDiff:
    """
    주문 하나의 변경. header 가 None 이어도 상세가 바뀌었으면 주문의 수집 ID 는 바꿉니다
    (상세가 바뀌면 상위 주문의 수집 ID 도 교체, serviceflow 5.2).
    """

    order_id: uuid.UUID
    record: OrderRecord
    header: RowChange[OrderRecord] | None
    lines: tuple[RowChange[OrderLineRecord], ...]
    records: tuple[ChangeRecordDraft, ...]


@dataclass(frozen=True)
class ShipmentDiff:
    """선적 하나의 변경. schedule_changed 면 일정 스냅샷을 새로 남깁니다 (지연 감지가 비교)"""

    shipment_id: uuid.UUID
    record: ShipmentRecord
    header: RowChange[ShipmentRecord] | None
    lines: tuple[RowChange[ShipmentLineRecord], ...]
    schedule_changed: bool
    records: tuple[ChangeRecordDraft, ...]


@dataclass(frozen=True)
class ExistingOrder:
    row_id: uuid.UUID
    record: OrderRecord
    line_ids: Mapping[int, uuid.UUID] = field(default_factory=dict)


@dataclass(frozen=True)
class ExistingShipment:
    row_id: uuid.UUID
    record: ShipmentRecord
    line_ids: Mapping[int, uuid.UUID] = field(default_factory=dict)


def diff_row[R](existing: Existing[R] | None, incoming: R) -> RowChange[R] | None:
    """업무 필드(FIELDS)를 비교해 INSERT·UPDATE 또는 변경 없음(None)을 돌려줍니다."""
    if existing is None:
        return RowChange("INSERT", uuid.uuid7(), incoming)
    changed = changed_fields(existing.record, incoming)
    if not changed:
        return None
    return RowChange("UPDATE", existing.row_id, incoming, changed)


def changed_fields(before: Any, after: Any) -> tuple[str, ...]:
    """FIELDS 순서대로 값이 다른 필드 이름 (DB 열 이름과 같음)"""
    return tuple(
        name
        for name in type(after).FIELDS
        if _normalize(getattr(before, name)) != _normalize(getattr(after, name))
    )


# --- 상품·창고·재고 ---------------------------------------------------------------


def diff_sku(
    existing: Existing[SkuRecord] | None, incoming: SkuRecord
) -> MasterDiff[SkuRecord] | None:
    row = diff_row(existing, incoming)
    if row is None:
        return None
    draft = ChangeRecordDraft(
        entity_type="SKU",
        entity_ref=row.row_id,
        change_type=row.change_type,
        sku_id=row.row_id,
        warehouse_id=None,
        changed_fields=_fields_or_none(row),
        change_summary=_summary(f"상품 {incoming.sku_code}", row),
    )
    return MasterDiff(row, (draft,))


def diff_warehouse(
    existing: Existing[WarehouseRecord] | None, incoming: WarehouseRecord
) -> MasterDiff[WarehouseRecord] | None:
    row = diff_row(existing, incoming)
    if row is None:
        return None
    draft = ChangeRecordDraft(
        entity_type="WAREHOUSE",
        entity_ref=row.row_id,
        change_type=row.change_type,
        sku_id=None,
        warehouse_id=row.row_id,
        changed_fields=_fields_or_none(row),
        change_summary=_summary(f"창고 {incoming.warehouse_code}", row),
    )
    return MasterDiff(row, (draft,))


def diff_inventory(
    existing: Existing[InventoryRecord] | None, incoming: InventoryRecord
) -> MasterDiff[InventoryRecord] | None:
    row = diff_row(existing, incoming)
    if row is None:
        return None
    label = f"재고 {incoming.inventory_date.isoformat()} {incoming.warehouse_code} {incoming.sku_code}"
    draft = ChangeRecordDraft(
        entity_type="INVENTORY",
        entity_ref=row.row_id,
        change_type=row.change_type,
        sku_id=incoming.sku_id,
        warehouse_id=incoming.warehouse_id,
        changed_fields=_fields_or_none(row),
        change_summary=_summary(label, row),
    )
    return MasterDiff(row, (draft,))


# --- 주문 -------------------------------------------------------------------------


def diff_order(
    existing: ExistingOrder | None, incoming: OrderRecord
) -> OrderDiff | None:
    """
    주문 머리와 상세를 함께 비교합니다. 원천 응답에 없는 기존 상세는 그대로 둡니다 (삭제로 보지 않음).
    머리 변경 기록은 상세의 SKU 마다 남기므로, 상세가 하나도 없는 주문은 머리가 바뀌어도 변경 기록이 0건입니다.
    의도한 동작입니다: 변경 기록은 SKU 가 필수이고(data_change_records_ck3), 상세 없는 주문은 재고를 쓰지 않아
    분석 결과(SKU 기준)에 영향이 없으므로 STALE 판정 대상이 아닙니다. 행 자체는 그대로 갱신합니다.
    """
    header = diff_row(
        Existing(existing.row_id, existing.record) if existing else None, incoming
    )
    order_id = existing.row_id if existing else header.row_id
    old_lines = (
        {line.line_no: line for line in existing.record.lines} if existing else {}
    )
    line_ids = existing.line_ids if existing else {}

    lines: list[RowChange[OrderLineRecord]] = []
    records: list[ChangeRecordDraft] = []
    for line in incoming.lines:
        old = old_lines.get(line.line_no)
        change = diff_row(Existing(line_ids[line.line_no], old) if old else None, line)
        if change is None:
            continue
        lines.append(change)
        label = f"주문 상세 {incoming.order_code}#{line.line_no}"
        summary = (
            "출고 수량"
            if change.changed_fields == ("shipped_quantity",)
            else _summary(label, change)
        )
        records += [
            ChangeRecordDraft(
                entity_type="ORDER_LINE",
                entity_ref=change.row_id,
                change_type=change.change_type,
                sku_id=sku_id,
                warehouse_id=incoming.warehouse_id,
                changed_fields=_fields_or_none(change),
                change_summary=summary,
            )
            for sku_id in _skus_before_after(old, line)
        ]

    if header is None and not lines:
        return None
    if header is not None:
        if header.changed_fields == ("status",):
            summary = f"주문 상태 {incoming.status}"
        else:
            summary = _summary(f"주문 {incoming.order_code}", header)
        skus = _distinct_skus(existing.record.lines if existing else (), incoming.lines)
        records[:0] = [
            ChangeRecordDraft(
                entity_type="ORDER",
                entity_ref=order_id,
                change_type=header.change_type,
                sku_id=sku_id,
                warehouse_id=incoming.warehouse_id,
                changed_fields=_fields_or_none(header),
                change_summary=summary,
            )
            for sku_id in skus
        ]
    return OrderDiff(order_id, incoming, header, tuple(lines), tuple(records))


# --- 선적 -------------------------------------------------------------------------


def diff_shipment(
    existing: ExistingShipment | None, incoming: ShipmentRecord
) -> ShipmentDiff | None:
    """선적 머리와 상세를 함께 비교합니다. 새 운송 중 선적이거나 단계·예상일이 바뀌면 일정 스냅샷을 남깁니다."""
    header = diff_row(
        Existing(existing.row_id, existing.record) if existing else None, incoming
    )
    shipment_id = existing.row_id if existing else header.row_id
    old_lines = (
        {line.line_no: line for line in existing.record.lines} if existing else {}
    )
    line_ids = existing.line_ids if existing else {}

    lines: list[RowChange[ShipmentLineRecord]] = []
    records: list[ChangeRecordDraft] = []
    for line in incoming.lines:
        old = old_lines.get(line.line_no)
        change = diff_row(Existing(line_ids[line.line_no], old) if old else None, line)
        if change is None:
            continue
        lines.append(change)
        label = f"선적 상세 {incoming.shipment_code}#{line.line_no}"
        records += [
            ChangeRecordDraft(
                entity_type="SHIPMENT_LINE",
                entity_ref=change.row_id,
                change_type=change.change_type,
                sku_id=sku_id,
                warehouse_id=incoming.warehouse_id,
                changed_fields=_fields_or_none(change),
                change_summary=_summary(label, change),
            )
            for sku_id in _skus_before_after(old, line)
        ]

    if header is None and not lines:
        return None
    # 기준 스냅샷은 지켜볼 일정이 있는 새 선적만 남깁니다. 이미 입고 완료(RECEIVED)로 처음 들어온 선적은 남기지 않습니다
    # (합성데이터 결정 1-10 "초기 선적 스냅샷", 원문은 docs/data-sync/ingest.md 9절).
    schedule_changed = header is not None and (
        (header.change_type == "INSERT" and incoming.current_stage != RECEIVED)
        or any(name in SCHEDULE_FIELDS for name in header.changed_fields)
    )
    if header is not None:
        skus = _distinct_skus(existing.record.lines if existing else (), incoming.lines)
        records[:0] = [
            ChangeRecordDraft(
                entity_type="SHIPMENT",
                entity_ref=shipment_id,
                change_type=header.change_type,
                sku_id=sku_id,
                warehouse_id=incoming.warehouse_id,
                changed_fields=_fields_or_none(header),
                change_summary=f"선적 {incoming.shipment_code} {incoming.current_stage}",
            )
            for sku_id in skus
        ]
    return ShipmentDiff(
        shipment_id, incoming, header, tuple(lines), schedule_changed, tuple(records)
    )


# --- 공통 -------------------------------------------------------------------------


def _normalize(value: Any) -> Any:
    """형식 차이를 변경으로 오인하지 않게 문자열 앞뒤 공백을 무시합니다 (serviceflow 5.2 판정의 한계)."""
    return value.strip() if isinstance(value, str) else value


def _fields_or_none(row: RowChange) -> tuple[str, ...] | None:
    # data_change_records_ck5: changed_fields 는 UPDATE 일 때만 1개 이상
    return row.changed_fields if row.change_type == "UPDATE" else None


def _summary(label: str, row: RowChange) -> str:
    if row.change_type == "INSERT":
        return label
    return f"{label} 변경: {', '.join(row.changed_fields)}"


def _skus_before_after(
    old: OrderLineRecord | ShipmentLineRecord | None,
    new: OrderLineRecord | ShipmentLineRecord,
) -> list[uuid.UUID]:
    """상세 변경의 영향 SKU: SKU 가 바뀌면 변경 전·후 둘 다"""
    if old is None or old.sku_id == new.sku_id:
        return [new.sku_id]
    return [old.sku_id, new.sku_id]


def _distinct_skus(
    *line_groups: Iterable[OrderLineRecord | ShipmentLineRecord],
) -> list[uuid.UUID]:
    """머리 변경의 영향 SKU: 기존·새 상세의 SKU 전체 (나온 순서, 중복 제거)"""
    seen: dict[uuid.UUID, None] = {}
    for lines in line_groups:
        for line in lines:
            seen.setdefault(line.sku_id)
    return list(seen)
