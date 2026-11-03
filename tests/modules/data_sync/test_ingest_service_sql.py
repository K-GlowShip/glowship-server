import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.modules.data_sync.application.exception.data_sync_exceptions import (
    SyncInProgressException,
)
from app.modules.data_sync.application.ingest.code_resolver import (
    DUPLICATE_LINE_NO,
    UNKNOWN_SKU,
)
from app.modules.data_sync.application.ingest.ingest_service import (
    DUPLICATE_SOURCE_RECORD,
    DataIngestService,
)
from app.modules.data_sync.application.ingest.source_payload import (
    SourceInventory,
    SourceOrder,
    SourceOrderLine,
    SourceShipment,
    SourceShipmentLine,
)
from app.modules.data_sync.infrastructure.data_sync_enums import (
    ConnectionMethod,
    DataSyncRunStatus,
    SourceDomain,
)
from app.modules.data_sync.infrastructure.repository.ingest_repository import (
    IngestRepository,
)
from app.modules.delay_analysis.application.stale.stale_service import (
    StaleJudgmentService,
)
from app.modules.delay_analysis.infrastructure.repository.stale_change_repository import (
    StaleChangeRepository,
)

"""
    수집 적재 서비스 통합 테스트 (실제 DB, 끝나면 롤백)
      - 서비스는 직접 commit·rollback 합니다. db_write_session 은 SAVEPOINT 모드라 테스트가 끝나면 모두 되돌립니다.
      - 2026 상반기 데이터의 실제 주문·선적을 원천 모양(외부 코드)으로 다시 만들어 넣습니다.
"""

NOW = datetime(2026, 7, 2, 1, 0, tzinfo=UTC)


class RecordingStale:
    """STALE 판정 호출만 기록하는 대역 (판정 자체는 GLOW-87 테스트가 검증)"""

    def __init__(self):
        self.calls: list[tuple[uuid.UUID, int]] = []

    async def judge_after_sync(self, company_id, applied_seq, detected_at):
        self.calls.append((company_id, applied_seq))
        return []


def _service(db, stale=None, repo=None) -> DataIngestService:
    return DataIngestService(
        db, repo or IngestRepository(db), stale or RecordingStale(), now=lambda: NOW
    )


async def _company(db) -> uuid.UUID:
    company_id = await db.scalar(text("SELECT company_id FROM orders LIMIT 1"))
    if company_id is None:
        pytest.skip("주문 데이터가 없는 DB")
    return company_id


async def _source_orders(db, company_id, *, status=None, limit=5) -> list[SourceOrder]:
    """DB 주문을 ERP 원천 모양(내부 코드)으로 다시 만듭니다."""
    repo = IngestRepository(db)
    codes = list(
        await db.scalars(
            text(
                "SELECT order_code FROM orders WHERE company_id = :c"
                " AND (CAST(:s AS text) IS NULL OR status = :s) ORDER BY order_code LIMIT :n"
            ),
            {"c": company_id, "s": status, "n": limit},
        )
    )
    sku_codes = dict(
        (await db.execute(text("SELECT sku_id, sku_code FROM skus"))).all()
    )
    warehouse_codes = dict(
        (
            await db.execute(
                text("SELECT warehouse_id, warehouse_code FROM warehouses")
            )
        ).all()
    )
    out = []
    for existing in (await repo.load_orders(company_id, codes)).values():
        r = existing.record
        out.append(
            SourceOrder(
                order_code=r.order_code,
                sales_channel=r.sales_channel,
                customer_region=r.customer_region,
                warehouse_code=warehouse_codes[r.warehouse_id],
                ordered_at=r.ordered_at,
                promised_delivery_at=r.promised_delivery_at,
                status=r.status,
                lines=tuple(
                    SourceOrderLine(
                        line.line_no,
                        sku_codes[line.sku_id],
                        line.ordered_quantity,
                        line.shipped_quantity,
                        line.line_status,
                    )
                    for line in r.lines
                ),
            )
        )
    return out


async def _run_row(db, run_id):
    return (
        await db.execute(
            text(
                "SELECT status, received_record_count, loaded_record_count,"
                " rejected_record_count, applied_seq FROM data_sync_runs"
                " WHERE data_sync_run_id = :r"
            ),
            {"r": run_id},
        )
    ).one()


async def _max_seq(db, company_id) -> int:
    return await db.scalar(
        text(
            "SELECT COALESCE(MAX(applied_seq), 0) FROM data_sync_runs WHERE company_id = :c"
        ),
        {"c": company_id},
    )


# --- 같은 값 재수집 ----------------------------------------------------------------


async def test_same_orders_again_change_nothing_but_take_a_sequence(db_write_session):
    db = db_write_session
    company_id = await _company(db)
    orders = await _source_orders(db, company_id)
    before = await _max_seq(db, company_id)
    stale = RecordingStale()

    result = await _service(db, stale).ingest(
        company_id, SourceDomain.ORDER, ConnectionMethod.API, orders
    )

    assert result.status == DataSyncRunStatus.SUCCEEDED
    assert result.change_record_count == 0
    assert result.applied_seq == before + 1
    assert tuple(await _run_row(db, result.data_sync_run_id)) == (
        "SUCCEEDED",
        len(orders),
        len(orders),
        0,
        before + 1,
    )
    assert stale.calls == [(company_id, before + 1)]


# --- 변경 적용 -----------------------------------------------------------------------


async def test_shipped_status_is_applied_with_change_records(db_write_session):
    db = db_write_session
    company_id = await _company(db)
    [order] = await _source_orders(db, company_id, status="OPEN", limit=1) or [None]
    if order is None:
        pytest.skip("OPEN 주문이 없음")

    result = await _service(db).ingest(
        company_id,
        SourceDomain.ORDER,
        ConnectionMethod.API,
        [replace(order, status="SHIPPED")],
    )

    status, row_run = (
        await db.execute(
            text(
                "SELECT status, data_sync_run_id FROM orders WHERE order_code = :o AND company_id = :c"
            ),
            {"o": order.order_code, "c": company_id},
        )
    ).one()
    assert (status, row_run) == ("SHIPPED", result.data_sync_run_id)
    summaries = set(
        await db.scalars(
            text(
                "SELECT change_summary FROM data_change_records WHERE data_sync_run_id = :r"
            ),
            {"r": result.data_sync_run_id},
        )
    )
    assert summaries == {"주문 상태 SHIPPED"}
    assert result.change_record_count == len({line.sku_code for line in order.lines})


# --- 거부 ------------------------------------------------------------------------------


async def test_unmapped_and_duplicate_records_make_partial_success(db_write_session):
    db = db_write_session
    company_id = await _company(db)
    good, dup = (await _source_orders(db, company_id, limit=2))[:2]
    bad = replace(
        good,
        order_code="TEST-BAD",
        lines=(SourceOrderLine(1, "NO-SUCH-SKU", 1, 0, "ACTIVE"),),
    )

    result = await _service(db).ingest(
        company_id, SourceDomain.ORDER, ConnectionMethod.FILE, [good, bad, dup, dup]
    )

    # 중복 dup 2건 중 첫 건은 적재, 두 번째만 거부
    assert result.status == DataSyncRunStatus.PARTIAL_SUCCESS
    assert (result.counts.received, result.counts.loaded, result.counts.rejected) == (
        4,
        2,
        2,
    )
    assert sorted((r.source_record_key, r.error_code) for r in result.rejections) == [
        (dup.order_code, DUPLICATE_SOURCE_RECORD),
        ("TEST-BAD", UNKNOWN_SKU),
    ]
    assert (await _run_row(db, result.data_sync_run_id)).status == "PARTIAL_SUCCESS"


async def test_duplicate_records_load_the_first_one(db_write_session):
    """값이 다른 중복이면 첫 레코드의 값이 적재되고, 나머지 2건은 1건씩 거부 (기준선 "첫 레코드만 적재")"""
    db = db_write_session
    company_id = await _company(db)
    [order] = await _source_orders(db, company_id, status="OPEN", limit=1) or [None]
    if order is None:
        pytest.skip("OPEN 주문이 없음")
    first = replace(order, status="SHIPPED")

    result = await _service(db).ingest(
        company_id, SourceDomain.ORDER, ConnectionMethod.FILE, [first, order, order]
    )

    assert (result.counts.received, result.counts.loaded, result.counts.rejected) == (
        3,
        1,
        2,
    )
    assert [r.error_code for r in result.rejections] == [DUPLICATE_SOURCE_RECORD] * 2
    status = await db.scalar(
        text("SELECT status FROM orders WHERE order_code = :o AND company_id = :c"),
        {"o": order.order_code, "c": company_id},
    )
    assert status == "SHIPPED"


async def test_duplicate_inventory_key_uses_baseline_format(db_write_session):
    """재고 중복 거부 키도 `창고 외부 코드|내부 SKU 코드|일자` (매핑된 창고도 외부 코드, 기준선 WMS-EWR01|SKU-215|2026-01-01)"""
    db = db_write_session
    row = (
        await db.execute(
            text(
                "SELECT w.company_id, w.external_code AS wh, s.external_code AS sku, k.sku_code"
                " FROM external_entity_mappings w"
                " JOIN external_entity_mappings s ON s.company_id = w.company_id"
                "  AND s.source_domain = 'INVENTORY' AND s.entity_type = 'SKU'"
                "  AND s.mapping_status = 'MAPPED'"
                " JOIN skus k ON k.sku_id = s.sku_id"
                " WHERE w.source_domain = 'INVENTORY' AND w.entity_type = 'WAREHOUSE'"
                "  AND w.mapping_status = 'MAPPED'"
                " ORDER BY w.external_code, s.external_code LIMIT 1"
            )
        )
    ).one_or_none()
    if row is None:
        pytest.skip("재고 매핑이 없는 DB")
    record = SourceInventory(
        inventory_date=datetime(2026, 7, 1).date(),
        warehouse_code=row.wh,
        sku_code=row.sku,
        on_hand_quantity=10,
        reserved_quantity=0,
        available_quantity=10,
        outbound_quantity=0,
    )

    result = await _service(db).ingest(
        row.company_id, SourceDomain.INVENTORY, ConnectionMethod.FILE, [record, record]
    )

    assert result.status == DataSyncRunStatus.PARTIAL_SUCCESS
    assert {(r.source_record_key, r.error_code) for r in result.rejections} == {
        (f"{row.wh}|{row.sku_code}|2026-07-01", DUPLICATE_SOURCE_RECORD)
    }


# --- 실패 ------------------------------------------------------------------------------


class FailingRepository(IngestRepository):
    async def apply_orders(self, company_id, data_sync_run_id, diffs):
        await super().apply_orders(company_id, data_sync_run_id, diffs)
        raise RuntimeError("적용 중 실패")


async def test_failure_rolls_back_rows_and_marks_run_failed(db_write_session):
    db = db_write_session
    company_id = await _company(db)
    [order] = await _source_orders(db, company_id, status="OPEN", limit=1) or [None]
    if order is None:
        pytest.skip("OPEN 주문이 없음")
    service = _service(db, repo=FailingRepository(db))

    with pytest.raises(RuntimeError):
        await service.ingest(
            company_id,
            SourceDomain.ORDER,
            ConnectionMethod.API,
            [replace(order, status="SHIPPED")],
        )

    status = await db.scalar(
        text("SELECT status FROM orders WHERE order_code = :o AND company_id = :c"),
        {"o": order.order_code, "c": company_id},
    )
    assert status == "OPEN"
    run = (
        await db.execute(
            text(
                "SELECT status, applied_seq FROM data_sync_runs WHERE company_id = :c"
                " AND started_at = :t AND source_domain = 'ORDER'"
            ),
            {"c": company_id, "t": NOW},
        )
    ).one()
    assert tuple(run) == ("FAILED", None)


# --- 선적: 새 선적의 계획 기준과 기준 스냅샷 ------------------------------------------------


async def _tms_shipment(db, company_id) -> SourceShipment:
    """DB 선적 하나를 TMS 원천 모양으로 만듭니다 (항구·기준·계획일 없음)."""
    repo = IngestRepository(db)
    book = await repo.load_code_book(company_id, SourceDomain.SHIPMENT)
    tms_sku = {code.sku_id: external for external, code in book.skus.items()}
    tms_warehouse = {
        code.warehouse_id: external for external, code in book.warehouses.items()
    }
    code = await db.scalar(
        text(
            "SELECT shipment_code FROM shipments WHERE company_id = :c ORDER BY shipment_code LIMIT 1"
        ),
        {"c": company_id},
    )
    record = (await repo.load_shipments(company_id, [code]))[code].record
    return SourceShipment(
        shipment_code=record.shipment_code,
        transport_mode=record.transport_mode,
        warehouse_code=tms_warehouse[record.warehouse_id],
        planned_kr_release_date=record.planned_kr_release_date,
        current_stage=record.current_stage,
        current_expected_port_arrival_date=record.current_expected_port_arrival_date,
        current_expected_3pl_inbound_date=record.current_expected_3pl_inbound_date,
        actual_3pl_inbound_date=record.actual_3pl_inbound_date,
        lines=tuple(
            SourceShipmentLine(
                line.line_no, tms_sku[line.sku_id], line.shipped_quantity
            )
            for line in record.lines
        ),
    )


async def _snapshot_count(db, run_id) -> int:
    return await db.scalar(
        text(
            "SELECT count(*) FROM shipment_schedule_snapshots WHERE data_sync_run_id = :r"
        ),
        {"r": run_id},
    )


async def test_new_in_transit_shipment_gets_plan_and_snapshot(db_write_session):
    db = db_write_session
    company_id = await _company(db)
    template = await _tms_shipment(db, company_id)
    in_transit = replace(
        template,
        shipment_code=f"TEST-{uuid.uuid7().hex[-10:]}",
        current_stage="AT_SEA",
        actual_3pl_inbound_date=None,
    )
    received = replace(
        template,
        shipment_code=f"TEST-{uuid.uuid7().hex[-10:]}",
        current_stage="RECEIVED",
        actual_3pl_inbound_date=template.current_expected_3pl_inbound_date,
    )

    result = await _service(db).ingest(
        company_id, SourceDomain.SHIPMENT, ConnectionMethod.API, [in_transit, received]
    )

    assert result.status == DataSyncRunStatus.SUCCEEDED, result.rejections
    snapshots = (
        await db.execute(
            text(
                "SELECT h.shipment_code, x.stage, x.observed_at FROM shipment_schedule_snapshots x"
                " JOIN shipments h USING (shipment_id) WHERE x.data_sync_run_id = :r"
            ),
            {"r": result.data_sync_run_id},
        )
    ).all()
    assert [(s.shipment_code, s.stage) for s in snapshots] == [
        (in_transit.shipment_code, "AT_SEA")
    ]
    assert snapshots[0].observed_at == NOW
    planned = await db.scalar(
        text("SELECT planned_3pl_inbound_date FROM shipments WHERE shipment_code = :s"),
        {"s": in_transit.shipment_code},
    )
    existing_planned = await db.scalar(
        text("SELECT planned_3pl_inbound_date FROM shipments WHERE shipment_code = :s"),
        {"s": template.shipment_code},
    )
    assert planned == existing_planned  # 같은 출고일·경로 → 같은 기준·계획일


async def test_existing_shipment_keeps_plan_when_release_date_changes(db_write_session):
    db = db_write_session
    company_id = await _company(db)
    source = await _tms_shipment(db, company_id)
    before = (
        await db.execute(
            text(
                "SELECT market_leadtime_reference_id, planned_3pl_inbound_date FROM shipments"
                " WHERE shipment_code = :s AND company_id = :c"
            ),
            {"s": source.shipment_code, "c": company_id},
        )
    ).one()
    moved = replace(
        source,
        planned_kr_release_date=source.planned_kr_release_date + timedelta(days=40),
    )

    result = await _service(db).ingest(
        company_id, SourceDomain.SHIPMENT, ConnectionMethod.API, [moved]
    )

    after = (
        await db.execute(
            text(
                "SELECT market_leadtime_reference_id, planned_3pl_inbound_date, planned_kr_release_date"
                " FROM shipments WHERE shipment_code = :s AND company_id = :c"
            ),
            {"s": source.shipment_code, "c": company_id},
        )
    ).one()
    assert tuple(after[:2]) == tuple(before)
    assert after.planned_kr_release_date == moved.planned_kr_release_date
    assert await _snapshot_count(db, result.data_sync_run_id) == 0  # 일정 필드는 그대로


# --- 실제 STALE 판정 서비스와 연결 ----------------------------------------------------------


async def test_ingest_calls_real_stale_judgment(db_write_session):
    db = db_write_session
    company_id = await _company(db)
    orders = await _source_orders(db, company_id, limit=2)
    stale = StaleJudgmentService(db, StaleChangeRepository(db))

    result = await _service(db, stale).ingest(
        company_id, SourceDomain.ORDER, ConnectionMethod.API, orders
    )

    assert result.status == DataSyncRunStatus.SUCCEEDED
    assert (
        result.staled_run_ids == ()
    )  # 확정 데이터의 지연은 모두 종료되어 판정 대상이 없음


# --- 같은 원천 동시 수집 ------------------------------------------------------------


async def _insert_running(
    db, company_id, domain: str, started_at: datetime
) -> uuid.UUID:
    run_id = uuid.uuid7()
    await db.execute(
        text(
            "INSERT INTO data_sync_runs (data_sync_run_id, company_id, source_domain,"
            " connection_method, started_at, status) VALUES (:r, :c, :d, 'API', :t, 'RUNNING')"
        ),
        {"r": run_id, "c": company_id, "d": domain, "t": started_at},
    )
    await (
        db.commit()
    )  # 다른 프로세스가 이미 커밋해 둔 RUNNING 처럼 (테스트 끝에 바깥 트랜잭션이 롤백)
    return run_id


async def _run_count(db, company_id) -> int:
    return await db.scalar(
        text("SELECT count(*) FROM data_sync_runs WHERE company_id = :c"),
        {"c": company_id},
    )


@pytest.mark.parametrize("age", [timedelta(seconds=1), timedelta(days=3)])
async def test_running_sync_of_same_source_rejects_new_sync(db_write_session, age):
    """RUNNING 이 있으면 시작 시각과 관계없이 거부 (오래 남은 RUNNING 도 막음, 정리는 후속 운영)"""
    db = db_write_session
    company_id = await _company(db)
    await _insert_running(db, company_id, "ORDER", NOW - age)
    orders = await _source_orders(db, company_id, limit=1)
    before = await _run_count(db, company_id)

    with pytest.raises(SyncInProgressException) as error:
        await _service(db).ingest(
            company_id, SourceDomain.ORDER, ConnectionMethod.API, orders
        )

    assert error.value.error_code.code == "SYNC_IN_PROGRESS"
    assert await _run_count(db, company_id) == before  # 새 실행을 남기지 않음


async def test_running_sync_of_other_source_does_not_block(db_write_session):
    db = db_write_session
    company_id = await _company(db)
    await _insert_running(db, company_id, "INVENTORY", NOW)
    orders = await _source_orders(db, company_id, limit=1)

    result = await _service(db).ingest(
        company_id, SourceDomain.ORDER, ConnectionMethod.API, orders
    )

    assert result.status == DataSyncRunStatus.SUCCEEDED


async def test_finished_sync_does_not_block_next_one(db_write_session):
    db = db_write_session
    company_id = await _company(db)
    orders = await _source_orders(db, company_id, limit=1)
    service = _service(db)

    first = await service.ingest(
        company_id, SourceDomain.ORDER, ConnectionMethod.API, orders
    )
    second = await service.ingest(
        company_id, SourceDomain.ORDER, ConnectionMethod.API, orders
    )

    assert second.applied_seq == first.applied_seq + 1


# --- 상세 줄 번호 중복 -----------------------------------------------------------------


async def test_duplicate_line_no_rejects_one_parent_without_failing_the_sync(
    db_write_session,
):
    """상세 줄 번호가 여러 개 겹쳐도 부모 주문 1건만 거부: 받은 = 적재 + 거부 (최상위 레코드 기준)"""
    db = db_write_session
    company_id = await _company(db)
    good, template = await _source_orders(db, company_id, limit=2)
    first = template.lines[0]
    broken = replace(
        template,
        order_code=f"TEST-{uuid.uuid7().hex[-10:]}",
        lines=(
            first,
            first,
            replace(first, line_no=first.line_no + 1),
            replace(first, line_no=first.line_no + 1),
        ),
    )

    result = await _service(db).ingest(
        company_id, SourceDomain.ORDER, ConnectionMethod.API, [good, broken]
    )

    assert result.status == DataSyncRunStatus.PARTIAL_SUCCESS
    assert (result.counts.received, result.counts.loaded, result.counts.rejected) == (
        2,
        1,
        1,
    )
    [rejection] = result.rejections
    assert (rejection.source_record_key, rejection.error_code) == (
        broken.order_code,
        DUPLICATE_LINE_NO,
    )
    row = await _run_row(db, result.data_sync_run_id)
    assert (
        row.received_record_count,
        row.loaded_record_count,
        row.rejected_record_count,
    ) == (2, 1, 1)
    assert (
        await db.scalar(
            text("SELECT count(*) FROM orders WHERE order_code = :o"),
            {"o": broken.order_code},
        )
        == 0
    )


async def test_duplicate_shipment_line_no_rejects_the_shipment(db_write_session):
    db = db_write_session
    company_id = await _company(db)
    template = await _tms_shipment(db, company_id)
    broken = replace(
        template,
        shipment_code=f"TEST-{uuid.uuid7().hex[-10:]}",
        lines=(template.lines[0], template.lines[0]),
    )

    result = await _service(db).ingest(
        company_id, SourceDomain.SHIPMENT, ConnectionMethod.API, [broken]
    )

    assert (result.counts.received, result.counts.loaded, result.counts.rejected) == (
        1,
        0,
        1,
    )
    assert result.rejections[0].error_code == DUPLICATE_LINE_NO
