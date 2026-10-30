import uuid
from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

from app.modules.delay_analysis.infrastructure.repository.stale_change_repository import (
    ScopedChange,
)

"""
    STALE 1차 판정: 변경 기록이 실행의 의존 범위에 해당하는지 (D-13, 정책 결정 ST-1·ST-2·ST-3)
      - 완료된 실행은 dependency_scope 로, 진행 중 실행은 지연 선적 SKU 전체(K_SHIPMENT)로 보수적으로 판정합니다 (Q-13a).
      - 결과는 해당한 사유 코드 목록입니다. 2차(판정 서명 재계산)는 서비스가 이어서 합니다.
      - 기준은 데이터팀 엔진 tools/glowship_engine/stale.py first_filter 와 같고,
        의존 계약의 SKU 속성 규칙(RELEVANT_SKU_ATTRIBUTE_CHANGED)을 더했습니다.
      - 지연 선적 자체의 일정·단계 변경은 여기서 다루지 않습니다. 후속 AUTO 실행이 DELAY_* 사유로 처리합니다 (D-08).
      - 운영 기준은 행을 고치지 않고 새 버전을 만드는 원칙이라 변경 기록에 나타나지 않습니다.
"""

ORDER_CHANGED = "RELEVANT_ORDER_CHANGED"
INVENTORY_CHANGED = "RELEVANT_INVENTORY_CHANGED"
INBOUND_CHANGED = "RELEVANT_INBOUND_CHANGED"
SKU_ATTRIBUTE_CHANGED = "RELEVANT_SKU_ATTRIBUTE_CHANGED"

# 예정된 정상 출고·배송 완료로 바뀐 주문은 결과를 바꾸지 않으므로 관련 변경이 아닙니다 (ST-2)
_PLANNED_SHIPMENT_MARKERS = ("SHIPPED", "DELIVERED", "출고 수량")


def relevant_reasons(
    scope: dict[str, Any], changes: Iterable[ScopedChange]
) -> list[str]:
    """완료된 실행(dependency_scope 있음)에 대해, 한 수집의 변경 기록 중 의존 범위에 해당한 사유 코드를 반환합니다."""
    base_date = date.fromisoformat(scope["base_date"])
    horizon_to = date.fromisoformat(scope["horizon"]["to"])
    order_scope = _pairs(scope["order_scope"])
    inventory_scope = _pairs(scope["inventory_scope"])
    inbound_warehouses = {
        uuid.UUID(scope["warehouses"]["destination"]),
        uuid.UUID(scope["warehouses"]["donor"]),
    }
    sku_sets = scope["sku_sets"]
    dependent_skus = {
        uuid.UUID(s) for s in sku_sets["k_impacted"] + sku_sets["k_watch"]
    }
    delayed_shipment_id = uuid.UUID(scope["entities"]["delayed_shipment_id"])
    sku_attribute_fields = set(scope.get("sku_attribute_fields", ()))

    reasons = set()
    for change in changes:
        entity_type = change.entity_type
        pair = (change.warehouse_id, change.sku_id)
        if entity_type in ("ORDER", "ORDER_LINE"):
            relevant = pair in order_scope and _is_relevant_order(
                change, base_date, horizon_to
            )
            reason = ORDER_CHANGED
        elif entity_type == "INVENTORY":
            relevant = pair in inventory_scope and _is_relevant_inventory(
                change, base_date
            )
            reason = INVENTORY_CHANGED
        elif entity_type in ("SHIPMENT", "SHIPMENT_LINE"):
            relevant = (
                change.shipment_id is not None
                and change.shipment_id != delayed_shipment_id
                and change.shipment_warehouse_id in inbound_warehouses
                and change.sku_id in dependent_skus
                and _is_relevant_inbound(change)
            )
            reason = INBOUND_CHANGED
        elif entity_type == "SKU":
            relevant = change.sku_id in dependent_skus and bool(
                sku_attribute_fields.intersection(change.changed_fields)
            )
            reason = SKU_ATTRIBUTE_CHANGED
        else:
            continue
        if relevant:
            reasons.add(reason)
    return sorted(reasons)


def relevant_reasons_in_progress(
    k_shipment: Iterable[uuid.UUID],
    delayed_shipment_id: uuid.UUID,
    base_date: date,
    changes: Iterable[ScopedChange],
) -> list[str]:
    """진행 중 실행(의존 범위 미확정)의 보수적 판정. 지연 선적 SKU 전체에 닿은 변경을 관련으로 봅니다."""
    skus = set(k_shipment)
    reasons = set()
    for change in changes:
        if change.sku_id not in skus:
            continue
        entity_type = change.entity_type
        if entity_type in ("ORDER", "ORDER_LINE"):
            if not _is_planned_shipment(change):
                reasons.add(ORDER_CHANGED)
        elif entity_type == "INVENTORY":
            if _is_relevant_inventory(change, base_date):
                reasons.add(INVENTORY_CHANGED)
        elif entity_type in ("SHIPMENT", "SHIPMENT_LINE"):
            if change.shipment_id != delayed_shipment_id and _is_relevant_inbound(
                change
            ):
                reasons.add(INBOUND_CHANGED)
        elif entity_type == "SKU":
            reasons.add(SKU_ATTRIBUTE_CHANGED)
    return sorted(reasons)


def group_by_applied_seq(
    changes: Iterable[ScopedChange],
) -> dict[int, list[ScopedChange]]:
    """변경 기록을 수집(적용 순번)별로 묶습니다. 적용 순번 오름차순입니다."""
    groups: dict[int, list[ScopedChange]] = {}
    for change in sorted(changes, key=lambda c: c.applied_seq):
        groups.setdefault(change.applied_seq, []).append(change)
    return groups


def _pairs(items: Iterable[dict[str, str]]) -> set[tuple[uuid.UUID, uuid.UUID]]:
    return {(uuid.UUID(x["warehouse_id"]), uuid.UUID(x["sku_id"])) for x in items}


def _is_planned_shipment(change: ScopedChange) -> bool:
    return change.change_type == "UPDATE" and any(
        marker in change.change_summary for marker in _PLANNED_SHIPMENT_MARKERS
    )


def _is_relevant_order(change: ScopedChange, base_date: date, horizon_to: date) -> bool:
    if _is_planned_shipment(change):
        return False
    if change.change_type == "INSERT":
        # 신규 주문은 영향 기간 안에 들어온 것만 (주문일 = 수집일 전날)
        return base_date <= change.sync_date - timedelta(days=1) <= horizon_to
    return True


def _is_relevant_inventory(change: ScopedChange, base_date: date) -> bool:
    # 입력 확정일 이후 날짜의 새 재고 행은 원인이 아닙니다 (Q-13c)
    return not (
        change.change_type == "INSERT"
        and change.inventory_date is not None
        and change.inventory_date >= base_date
    )


def _is_relevant_inbound(change: ScopedChange) -> bool:
    if change.change_type == "INSERT":
        return True
    # 단계만 바뀐 변경은 제외하고 예상 입고일이 바뀐 것만 관련 (ST-2)
    if change.inbound_date_before is None or change.inbound_date_after is None:
        return False
    return change.inbound_date_before != change.inbound_date_after
