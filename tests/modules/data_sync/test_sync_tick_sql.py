from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.core.security.tenant import TenantContext
from app.modules.data_sync.application.exception.data_sync_exceptions import (
    SyncInProgressException,
)
from app.modules.data_sync.application.manual_sync_service import ManualSyncService
from app.modules.data_sync.infrastructure.virtual_sources import VirtualSourceScenario
from tests.modules.data_sync.test_manual_sync_sql import _a_beauty

"""
    고객사 tick: 다섯 원천을 함께, 버튼 한 번 = 가상 10분 (2026-11-09 결정) — 실제 DB, 끝나면 롤백
      - 확정 DB(마지막 수집 07-01)에서 첫 tick 은 07-02 00:00, 다음은 00:10. tick 마다 원천 5건이 상품 → 창고 → 주문 → 선적 → 재고
        순서로 시작(tick + 0~4분)하고, 시작 순서 = 적용 순번(이슈 #39).
      - 시험용 시나리오: 07-02 00:05 에 접수된 새 주문 1건(A_BEAUTY, 확정 주문과 같은 창고·SKU). 첫 tick 의 주문 구간
        (07-01 00:02, 07-02 00:02] 에는 없고, 둘째 tick (00:02, 00:12] 에 들어온다.
      - 워커 세션 대신 시험 세션을 넘긴다.
"""

START = datetime(2026, 7, 2, tzinfo=UTC)
CODE = "A-ORD-TICK-TEST-0001"


async def _order_payload(db, company_id) -> dict:
    row = (
        await db.execute(
            text(
                """
                SELECT w.warehouse_code, k.sku_code
                  FROM orders o JOIN order_lines l USING (order_id)
                  JOIN warehouses w ON w.warehouse_id = o.warehouse_id
                  JOIN skus k ON k.sku_id = l.sku_id
                 WHERE o.company_id = :c AND w.warehouse_code = 'WH-WEST' LIMIT 1
                """
            ),
            {"c": company_id},
        )
    ).one()
    return {
        "order_code": CODE,
        "sales_channel": "SEPHORA",
        "customer_region": "WEST",
        "warehouse_code": row.warehouse_code,
        "ordered_at": "2026-07-02T00:05:00Z",
        "promised_delivery_at": "2026-07-09T23:59:59Z",
        "status": "OPEN",
        "lines": [
            {
                "line_no": 1,
                "sku_code": row.sku_code,
                "ordered_quantity": 1,
                "shipped_quantity": 0,
                "line_status": "ACTIVE",
            }
        ],
    }


def _service(db, order) -> ManualSyncService:
    @asynccontextmanager
    async def same_session():
        yield db

    scenario = VirtualSourceScenario.from_json(
        {
            "responses": [
                {
                    "fetched_at": order["ordered_at"],
                    "_demo_company": "A_BEAUTY",
                    "source_domain": "ORDER",
                    "data": [order],
                }
            ]
        }
    )
    return ManualSyncService(
        open_session=same_session,
        virtual_start=START,
        load_scenario=lambda: None,
        load_sources=lambda: scenario,
    )


async def _runs(db, company_id, since):
    return (
        await db.execute(
            text(
                "SELECT source_domain, started_at, status, applied_seq, loaded_record_count"
                " FROM data_sync_runs WHERE company_id = :c AND started_at >= :s ORDER BY started_at"
            ),
            {"c": company_id, "s": since},
        )
    ).all()


async def test_two_ticks_sync_five_sources_in_order_ten_minutes_apart(db_write_session):
    db = db_write_session
    company_id = await _a_beauty(db)
    service = _service(db, await _order_payload(db, company_id))
    tenant = TenantContext(user_id=None, company_id=company_id)

    first = await service.request_tick(tenant)
    await service.run_tick(first)
    second = await service.request_tick(tenant)
    results = await service.run_tick(second)

    runs = await _runs(db, company_id, START)
    assert (first.plan.at, second.plan.at) == (
        datetime(2026, 7, 2, 0, 0, tzinfo=UTC),
        datetime(2026, 7, 2, 0, 10, tzinfo=UTC),
    )
    assert [
        (r.source_domain, r.started_at.strftime("%H:%M"), r.status) for r in runs
    ] == [
        ("PRODUCT", "00:00", "SUCCEEDED"),
        ("WAREHOUSE", "00:01", "SUCCEEDED"),
        ("ORDER", "00:02", "SUCCEEDED"),
        ("SHIPMENT", "00:03", "SUCCEEDED"),
        ("INVENTORY", "00:04", "SUCCEEDED"),
        ("PRODUCT", "00:10", "SUCCEEDED"),
        ("WAREHOUSE", "00:11", "SUCCEEDED"),
        ("ORDER", "00:12", "SUCCEEDED"),
        ("SHIPMENT", "00:13", "SUCCEEDED"),
        ("INVENTORY", "00:14", "SUCCEEDED"),
    ]
    assert [r.applied_seq for r in runs] == sorted(
        r.applied_seq for r in runs
    )  # 시작 순서 = 적용 순번
    assert [r.loaded_record_count for r in runs if r.source_domain == "ORDER"] == [0, 1]
    assert all(r is not None for r in results)
    assert (
        await db.scalar(
            text("SELECT status FROM orders WHERE order_code = :o"), {"o": CODE}
        )
        == "OPEN"
    )


async def test_tick_is_rejected_while_any_source_is_running(db_write_session):
    db = db_write_session
    company_id = await _a_beauty(db)
    service = _service(db, await _order_payload(db, company_id))
    tenant = TenantContext(user_id=None, company_id=company_id)
    await service.request_tick(tenant)  # 다섯 원천 RUNNING (적용 전)

    with pytest.raises(SyncInProgressException):
        await service.request_tick(tenant)

    running = await db.scalar(
        text(
            "SELECT count(*) FROM data_sync_runs WHERE company_id = :c AND status = 'RUNNING'"
        ),
        {"c": company_id},
    )
    assert running == 5  # 두 번째 요청은 아무것도 시작하지 않음
