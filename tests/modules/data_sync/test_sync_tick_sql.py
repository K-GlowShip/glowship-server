import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.core.security.tenant import TenantContext
from app.modules.data_sync.application.exception.data_sync_exceptions import (
    SyncInProgressException,
)
from app.modules.data_sync.application.manual_sync_service import (
    ManualSyncService,
    run_created_at,
)
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


async def test_start_error_midway_closes_started_sources_as_failed(
    db_write_session, monkeypatch
):
    """세 번째 원천 시작에서 DB 오류 등이 나면, 이미 시작한 두 원천을 FAILED 로 닫고 원래 오류를 올린다.
    RUNNING 이 남으면 그 고객사의 동기화가 모두 409 로 막힌다(PR #91 리뷰)."""
    from app.modules.data_sync.application.ingest.ingest_service import (
        DataIngestService,
    )

    db = db_write_session
    company_id = await _a_beauty(db)
    service = _service(db, await _order_payload(db, company_id))
    tenant = TenantContext(user_id=None, company_id=company_id)
    real_start = DataIngestService.start
    calls = {"n": 0}

    async def start_failing_third(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("시험용 DB 오류")
        return await real_start(self, *args, **kwargs)

    monkeypatch.setattr(DataIngestService, "start", start_failing_third)

    with pytest.raises(RuntimeError, match="시험용 DB 오류"):
        await service.request_tick(tenant)

    rows = await _runs(db, company_id, START)
    assert [(r.source_domain, r.status) for r in rows] == [
        ("PRODUCT", "FAILED"),
        ("WAREHOUSE", "FAILED"),
    ]


# --- 오래 남은 RUNNING 정리 (이슈 #94) -------------------------------------------------------

REAL_NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)  # 실제 시각(가상 시각 7월과 따로)


def _uuid7_at(at: datetime) -> uuid.UUID:
    """at 에 만든 것 같은 UUIDv7 (앞 48비트 = 실제 생성 시각 ms)."""
    ms = int(at.timestamp() * 1000)
    value = (ms << 80) | (0x7 << 76) | (uuid.uuid4().int & ((1 << 76) - 1))
    value = (value & ~(0x3 << 62)) | (0x2 << 62)  # RFC 9562 variant
    return uuid.UUID(int=value)


async def _insert_running(db, company_id, run_id, started_at):
    await db.execute(
        text(
            "INSERT INTO data_sync_runs (data_sync_run_id, company_id, source_domain, connection_method,"
            " started_at, status) VALUES (:r, :c, 'ORDER', 'API', :s, 'RUNNING')"
        ),
        {"r": run_id, "c": company_id, "s": started_at},
    )
    await db.commit()  # 실제로는 이미 커밋된 RUNNING 이다(409 경로의 되돌림이 지우지 않게). 시험 세션은 끝에서 모두 되돌림


def test_run_created_at_reads_real_time_from_uuid7():
    assert run_created_at(_uuid7_at(REAL_NOW)) == REAL_NOW
    assert run_created_at(uuid.uuid4()) is None  # UUIDv7 이 아니면 정리하지 않음


async def test_running_older_than_ten_real_minutes_is_closed_and_tick_proceeds(
    db_write_session,
):
    """서버 재시작·배포로 죽어 남은 RUNNING 이 있어도, 실제로 10분이 넘었으면 FAILED 로 닫고 동기화한다."""
    db = db_write_session
    company_id = await _a_beauty(db)
    stale = _uuid7_at(REAL_NOW - timedelta(minutes=11))
    await _insert_running(
        db, company_id, stale, datetime(2026, 7, 1, 23, 59, tzinfo=UTC)
    )
    service = _service(db, await _order_payload(db, company_id))
    service.now = lambda: REAL_NOW

    ticket = await service.request_tick(
        TenantContext(user_id=None, company_id=company_id)
    )

    row = (
        await db.execute(
            text(
                "SELECT status, finished_at FROM data_sync_runs WHERE data_sync_run_id = :r"
            ),
            {"r": stale},
        )
    ).one()
    assert row.status == "FAILED"
    assert row.finished_at == datetime(
        2026, 7, 1, 23, 59, 30, tzinfo=UTC
    )  # 가상 시각: 시작 + 30초
    assert len(ticket.started) == 5


async def test_running_younger_than_ten_real_minutes_still_blocks(db_write_session):
    """실제로 10분이 안 된 RUNNING 은 진행 중으로 보고 지금처럼 409, 그대로 남긴다."""
    db = db_write_session
    company_id = await _a_beauty(db)
    young = _uuid7_at(REAL_NOW - timedelta(minutes=1))
    await _insert_running(
        db, company_id, young, datetime(2026, 7, 1, 23, 59, tzinfo=UTC)
    )
    service = _service(db, await _order_payload(db, company_id))
    service.now = lambda: REAL_NOW

    with pytest.raises(SyncInProgressException):
        await service.request_tick(TenantContext(user_id=None, company_id=company_id))

    status = await db.scalar(
        text("SELECT status FROM data_sync_runs WHERE data_sync_run_id = :r"),
        {"r": young},
    )
    assert status == "RUNNING"


async def test_closed_stale_run_cannot_apply_later(db_write_session):
    """fencing: 느리게 살아 있던 작업의 실행이 정리로 FAILED 가 되면, 그 작업이 나중에 적용하려 해도 아무것도 들어가지 않는다.
    적용은 L1 안 한 트랜잭션이고 마지막 완료 기록이 RUNNING 인 실행만 바꾸므로(_finish) 통째로 되돌아간다."""
    db = db_write_session
    company_id = await _a_beauty(db)
    service = _service(db, await _order_payload(db, company_id))
    tenant = TenantContext(user_id=None, company_id=company_id)
    first = await service.request_tick(
        tenant
    )  # 다섯 원천 RUNNING (지금 실제 시각에 만든 ID)
    service.now = lambda: datetime.now(UTC) + timedelta(minutes=11)
    second = await service.request_tick(
        tenant
    )  # 11분 뒤 요청: 첫 tick 을 FAILED 로 닫고 진행

    late = await service.run_tick(first)  # 죽은 줄 알았던 첫 작업이 뒤늦게 적용 시도

    assert late == [None] * 5
    statuses = (
        (
            await db.execute(
                text(
                    "SELECT status FROM data_sync_runs WHERE data_sync_run_id = ANY(:ids)"
                ),
                {"ids": [r.data_sync_run_id for r in first.started]},
            )
        )
        .scalars()
        .all()
    )
    assert set(statuses) == {"FAILED"}
    assert len(second.started) == 5
