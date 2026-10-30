import uuid
from datetime import UTC, date, datetime

from app.modules.delay_analysis.application.stale.relevance import (
    INBOUND_CHANGED,
    INVENTORY_CHANGED,
    ORDER_CHANGED,
    SKU_ATTRIBUTE_CHANGED,
    group_by_applied_seq,
    relevant_reasons,
    relevant_reasons_in_progress,
)
from app.modules.delay_analysis.infrastructure.repository.stale_change_repository import (
    ScopedChange,
)

DEST, DONOR, OTHER_WH = uuid.uuid7(), uuid.uuid7(), uuid.uuid7()
SKU_IMPACTED, SKU_WATCH, SKU_OTHER = uuid.uuid7(), uuid.uuid7(), uuid.uuid7()
DELAYED_SHIPMENT, INBOUND_SHIPMENT = uuid.uuid7(), uuid.uuid7()
BASE_DATE = date(2026, 3, 1)

SCOPE = {
    "schema_version": "dependency_scope.v1",
    "base_date": BASE_DATE.isoformat(),
    "horizon": {"from": "2026-03-01", "to": "2026-03-31"},
    "order_scope": [{"warehouse_id": str(DEST), "sku_id": str(SKU_IMPACTED)}],
    "inventory_scope": [{"warehouse_id": str(DEST), "sku_id": str(SKU_IMPACTED)}],
    "warehouses": {
        "destination": str(DEST),
        "donor": str(DONOR),
        "kr": str(uuid.uuid7()),
    },
    "sku_sets": {
        "k_impacted": [str(SKU_IMPACTED)],
        "k_watch": [str(SKU_WATCH)],
        "k_shipment": [str(SKU_IMPACTED), str(SKU_WATCH), str(SKU_OTHER)],
    },
    "entities": {"delayed_shipment_id": str(DELAYED_SHIPMENT)},
    "sku_attribute_fields": ["emergency_air_eligible", "status"],
}


def _change(entity_type: str, change_type: str = "UPDATE", **kwargs) -> ScopedChange:
    values = {
        "applied_seq": 11,
        "sync_started_at": datetime(2026, 3, 5, 0, 3, tzinfo=UTC),
        "sync_finished_at": datetime(2026, 3, 5, 0, 4, tzinfo=UTC),
        "entity_type": entity_type,
        "change_type": change_type,
        "entity_ref": uuid.uuid7(),
        "sku_id": SKU_IMPACTED,
        "warehouse_id": DEST,
        "changed_fields": (),
        "change_summary": "변경",
    } | kwargs
    return ScopedChange(**values)


def _inbound(change_type: str = "UPDATE", **kwargs) -> ScopedChange:
    values = {
        "shipment_id": INBOUND_SHIPMENT,
        "shipment_warehouse_id": DEST,
        "inbound_date_before": date(2026, 3, 10),
        "inbound_date_after": date(2026, 3, 10),
    } | kwargs
    return _change("SHIPMENT", change_type, **values)


# --- 완료된 실행: 주문 -----------------------------------------------------------


def test_order_update_in_order_scope_is_relevant():
    assert relevant_reasons(SCOPE, [_change("ORDER_LINE")]) == [ORDER_CHANGED]


def test_order_outside_order_scope_is_not_relevant():
    changes = [
        _change("ORDER", warehouse_id=OTHER_WH),
        _change("ORDER", sku_id=SKU_OTHER),
    ]

    assert relevant_reasons(SCOPE, changes) == []


def test_planned_shipment_of_order_is_not_relevant():
    change = _change("ORDER", change_summary="주문 상태 SHIPPED")

    assert relevant_reasons(SCOPE, [change]) == []


def test_new_order_counts_only_inside_horizon():
    inside = _change("ORDER", "INSERT")  # 주문일 3/4 ∈ [3/1, 3/31]
    after = _change(
        "ORDER", "INSERT", sync_started_at=datetime(2026, 4, 10, tzinfo=UTC)
    )

    assert relevant_reasons(SCOPE, [inside]) == [ORDER_CHANGED]
    assert relevant_reasons(SCOPE, [after]) == []


# --- 완료된 실행: 재고 -----------------------------------------------------------


def test_inventory_correction_is_relevant_but_new_date_row_is_not():
    corrected = _change("INVENTORY", inventory_date=date(2026, 2, 28))
    late_insert = _change("INVENTORY", "INSERT", inventory_date=date(2026, 2, 27))
    new_date = _change("INVENTORY", "INSERT", inventory_date=BASE_DATE)

    assert relevant_reasons(SCOPE, [corrected]) == [INVENTORY_CHANGED]
    assert relevant_reasons(SCOPE, [late_insert]) == [INVENTORY_CHANGED]
    assert relevant_reasons(SCOPE, [new_date]) == []


# --- 완료된 실행: 입고 예정 선적 -------------------------------------------------


def test_inbound_expected_date_change_is_relevant_but_stage_only_is_not():
    moved = _inbound(inbound_date_after=date(2026, 3, 15))
    stage_only = _inbound()

    assert relevant_reasons(SCOPE, [moved]) == [INBOUND_CHANGED]
    assert relevant_reasons(SCOPE, [stage_only]) == []


def test_new_inbound_shipment_for_watch_sku_is_relevant():
    assert relevant_reasons(SCOPE, [_inbound("INSERT", sku_id=SKU_WATCH)]) == [
        INBOUND_CHANGED
    ]


def test_inbound_change_of_delayed_shipment_or_other_warehouse_is_not_relevant():
    delayed = _inbound("INSERT", shipment_id=DELAYED_SHIPMENT)
    other_wh = _inbound("INSERT", shipment_warehouse_id=OTHER_WH)
    other_sku = _inbound("INSERT", sku_id=SKU_OTHER)

    assert relevant_reasons(SCOPE, [delayed, other_wh, other_sku]) == []


# --- 완료된 실행: SKU 속성 -------------------------------------------------------


def test_sku_attribute_change_counts_only_for_used_fields():
    used = _change("SKU", warehouse_id=None, changed_fields=("status",))
    unused = _change("SKU", warehouse_id=None, changed_fields=("sku_name",))

    assert relevant_reasons(SCOPE, [used]) == [SKU_ATTRIBUTE_CHANGED]
    assert relevant_reasons(SCOPE, [unused]) == []


def test_reasons_are_sorted_and_unique():
    changes = [
        _change("ORDER"),
        _change("ORDER_LINE"),
        _change("INVENTORY", inventory_date=date(2026, 2, 1)),
    ]

    assert relevant_reasons(SCOPE, changes) == [INVENTORY_CHANGED, ORDER_CHANGED]


# --- 진행 중 실행 (K_SHIPMENT 보수 판정) -----------------------------------------


def test_in_progress_run_uses_all_shipment_skus():
    k_shipment = [SKU_IMPACTED, SKU_OTHER]
    changes = [
        _change("ORDER", sku_id=SKU_OTHER, warehouse_id=OTHER_WH),
        _change("INVENTORY", "INSERT", inventory_date=BASE_DATE),
        _change("ORDER", sku_id=SKU_WATCH),
    ]

    reasons = relevant_reasons_in_progress(
        k_shipment, DELAYED_SHIPMENT, BASE_DATE, changes
    )

    assert reasons == [ORDER_CHANGED]


# --- 수집별 묶기 -----------------------------------------------------------------


def test_group_by_applied_seq_orders_by_seq():
    changes = [_change("ORDER", applied_seq=13), _change("ORDER", applied_seq=12)]

    assert list(group_by_applied_seq(changes)) == [12, 13]
