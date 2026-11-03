import uuid
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from app.modules.data_sync.application.ingest.change_detection import (
    diff_inventory,
    diff_order,
    diff_shipment,
    diff_sku,
    diff_warehouse,
)
from app.modules.data_sync.application.ingest.code_resolver import resolve_shipment
from app.modules.data_sync.application.ingest.run_counts import RunCounts
from app.modules.data_sync.application.ingest.source_payload import (
    SourceShipment,
    SourceShipmentLine,
)
from app.modules.data_sync.application.ingest.source_records import OrderLineRecord
from app.modules.data_sync.infrastructure.data_change_record import DataChangeRecord
from app.modules.data_sync.infrastructure.data_sync_enums import (
    ConnectionMethod,
    SourceDomain,
)
from app.modules.data_sync.infrastructure.data_sync_run import DataSyncRun
from app.modules.data_sync.infrastructure.repository.ingest_repository import (
    IngestRepository,
)
from app.modules.order.infrastructure.order import Order
from app.modules.shipment.infrastructure.shipment_schedule_snapshot import (
    ShipmentScheduleSnapshot,
)

"""
    수집 적재 Repository SQL 테스트 (실제 DB, 쓰기 후 롤백)
      - 2026 상반기 데이터의 실제 행으로 "읽은 값 = 레코드" 와 적용 결과를 확인합니다.
      - db_write_session 은 테스트가 끝나면 롤백하므로 DB 는 바뀌지 않습니다. DB 가 없으면 건너뜁니다.
"""

NOW = datetime(2026, 7, 2, tzinfo=UTC)


async def _company(db) -> uuid.UUID:
    company_id = await db.scalar(text("SELECT company_id FROM orders LIMIT 1"))
    if company_id is None:
        pytest.skip("주문 데이터가 없는 DB 라 건너뜀")
    return company_id


async def _new_run(db, company_id: uuid.UUID, domain: str) -> uuid.UUID:
    run_id = uuid.uuid7()
    db.add(
        DataSyncRun(
            data_sync_run_id=run_id,
            company_id=company_id,
            source_domain=domain,
            connection_method="API",
            started_at=NOW,
            status="RUNNING",
        )
    )
    await db.flush()
    return run_id


async def _records_of(db, run_id: uuid.UUID) -> list[DataChangeRecord]:
    return list(
        await db.scalars(
            select(DataChangeRecord).where(DataChangeRecord.data_sync_run_id == run_id)
        )
    )


# --- 읽기: DB 값을 다시 넣으면 변경 없음 ---------------------------------------------


async def test_loaded_rows_round_trip_without_change(db_write_session):
    db = db_write_session
    repo = IngestRepository(db)
    company_id = await _company(db)
    sku_code, warehouse_code, order_code, shipment_code = (
        await db.execute(
            text(
                "SELECT (SELECT sku_code FROM skus WHERE company_id = :c LIMIT 1),"
                " (SELECT warehouse_code FROM warehouses WHERE company_id = :c LIMIT 1),"
                " (SELECT order_code FROM orders WHERE company_id = :c LIMIT 1),"
                " (SELECT shipment_code FROM shipments WHERE company_id = :c LIMIT 1)"
            ),
            {"c": company_id},
        )
    ).one()
    inventory_key = (
        await db.execute(
            text(
                "SELECT inventory_date, warehouse_id, sku_id FROM inventory_daily"
                " WHERE company_id = :c LIMIT 1"
            ),
            {"c": company_id},
        )
    ).one()

    sku = (await repo.load_skus(company_id, [sku_code]))[sku_code]
    warehouse = (await repo.load_warehouses(company_id, [warehouse_code]))[
        warehouse_code
    ]
    order = (await repo.load_orders(company_id, [order_code]))[order_code]
    shipment = (await repo.load_shipments(company_id, [shipment_code]))[shipment_code]
    key = tuple(inventory_key)
    inventory = (await repo.load_inventory(company_id, [key]))[key]

    assert diff_sku(sku, sku.record) is None
    assert diff_warehouse(warehouse, warehouse.record) is None
    assert diff_order(order, order.record) is None
    assert diff_shipment(shipment, shipment.record) is None
    assert diff_inventory(inventory, inventory.record) is None
    assert order.record.lines and set(order.line_ids) == {
        line.line_no for line in order.record.lines
    }


async def test_loads_are_scoped_by_company(db_write_session):
    db = db_write_session
    repo = IngestRepository(db)
    order_code = await db.scalar(text("SELECT order_code FROM orders LIMIT 1"))

    assert await repo.load_orders(uuid.uuid7(), [order_code]) == {}
    assert await repo.load_orders(uuid.uuid7(), []) == {}
    assert await repo.load_inventory(uuid.uuid7(), []) == {}


# --- 쓰기 -------------------------------------------------------------------------


async def test_order_status_update_changes_row_and_records(db_write_session):
    db = db_write_session
    repo = IngestRepository(db)
    company_id = await _company(db)
    order_code = await db.scalar(
        text(
            "SELECT order_code FROM orders WHERE company_id = :c AND status = 'OPEN' LIMIT 1"
        ),
        {"c": company_id},
    )
    if order_code is None:
        pytest.skip("OPEN 주문이 없음")
    run_id = await _new_run(db, company_id, "ORDER")
    existing = (await repo.load_orders(company_id, [order_code]))[order_code]

    diff = diff_order(existing, replace(existing.record, status="SHIPPED"))
    await repo.apply_orders(company_id, run_id, [diff])
    await repo.insert_change_records(company_id, run_id, diff.records)

    status, row_run_id = (
        await db.execute(
            text("SELECT status, data_sync_run_id FROM orders WHERE order_id = :o"),
            {"o": existing.row_id},
        )
    ).one()
    assert (status, row_run_id) == ("SHIPPED", run_id)
    records = await _records_of(db, run_id)
    assert {r.change_summary for r in records} == {"주문 상태 SHIPPED"}
    assert {r.sku_id for r in records} == {
        line.sku_id for line in existing.record.lines
    }
    assert all(r.changed_fields == ["status"] for r in records)


async def test_line_only_change_replaces_order_run_id(db_write_session):
    db = db_write_session
    repo = IngestRepository(db)
    company_id = await _company(db)
    order_code = await db.scalar(
        text("SELECT order_code FROM orders WHERE company_id = :c LIMIT 1"),
        {"c": company_id},
    )
    run_id = await _new_run(db, company_id, "ORDER")
    existing = (await repo.load_orders(company_id, [order_code]))[order_code]
    first = existing.record.lines[0]
    changed = replace(
        existing.record,
        lines=(
            replace(first, ordered_quantity=first.ordered_quantity + 1),
            *existing.record.lines[1:],
        ),
    )

    diff = diff_order(existing, changed)
    await repo.apply_orders(company_id, run_id, [diff])

    assert diff.header is None
    order_run = await db.scalar(
        text("SELECT data_sync_run_id FROM orders WHERE order_id = :o"),
        {"o": existing.row_id},
    )
    quantity = await db.scalar(
        text("SELECT ordered_quantity FROM order_lines WHERE order_line_id = :l"),
        {"l": existing.line_ids[first.line_no]},
    )
    assert (order_run, quantity) == (run_id, first.ordered_quantity + 1)


async def test_new_order_with_lines_is_inserted(db_write_session):
    db = db_write_session
    repo = IngestRepository(db)
    company_id = await _company(db)
    template_code = await db.scalar(
        text("SELECT order_code FROM orders WHERE company_id = :c LIMIT 1"),
        {"c": company_id},
    )
    template = (await repo.load_orders(company_id, [template_code]))[
        template_code
    ].record
    run_id = await _new_run(db, company_id, "ORDER")
    new_order = replace(
        template,
        order_code=f"TEST-{uuid.uuid7().hex[:12]}",
        status="OPEN",
        lines=(OrderLineRecord(1, template.lines[0].sku_id, 2, 0, "ACTIVE"),),
    )

    diff = diff_order(None, new_order)
    await repo.apply_orders(company_id, run_id, [diff])
    await repo.insert_change_records(company_id, run_id, diff.records)

    loaded = (await repo.load_orders(company_id, [new_order.order_code]))[
        new_order.order_code
    ]
    assert loaded.row_id == diff.order_id
    assert diff_order(loaded, new_order) is None
    assert [r.change_type for r in await _records_of(db, run_id)] == [
        "INSERT",
        "INSERT",
    ]


async def test_shipment_schedule_change_adds_snapshot(db_write_session):
    db = db_write_session
    repo = IngestRepository(db)
    company_id = await _company(db)
    shipment_code = await db.scalar(
        text("SELECT shipment_code FROM shipments WHERE company_id = :c LIMIT 1"),
        {"c": company_id},
    )
    run_id = await _new_run(db, company_id, "SHIPMENT")
    existing = (await repo.load_shipments(company_id, [shipment_code]))[shipment_code]
    later = existing.record.current_expected_3pl_inbound_date + timedelta(days=3)

    diff = diff_shipment(
        existing, replace(existing.record, current_expected_3pl_inbound_date=later)
    )
    await repo.apply_shipments(company_id, run_id, NOW, [diff])
    await repo.insert_change_records(company_id, run_id, diff.records)

    snapshot = (
        await db.scalars(
            select(ShipmentScheduleSnapshot).where(
                ShipmentScheduleSnapshot.data_sync_run_id == run_id
            )
        )
    ).one()
    assert (snapshot.shipment_id, snapshot.expected_3pl_inbound_date) == (
        existing.row_id,
        later,
    )
    assert snapshot.observed_at == NOW
    records = await _records_of(db, run_id)
    assert {r.entity_type for r in records} == {"SHIPMENT"}
    assert all(
        r.changed_fields == ["current_expected_3pl_inbound_date"] for r in records
    )


async def test_inventory_insert_and_update(db_write_session):
    db = db_write_session
    repo = IngestRepository(db)
    company_id = await _company(db)
    key = tuple(
        (
            await db.execute(
                text(
                    "SELECT inventory_date, warehouse_id, sku_id FROM inventory_daily"
                    " WHERE company_id = :c ORDER BY inventory_date DESC LIMIT 1"
                ),
                {"c": company_id},
            )
        ).one()
    )
    run_id = await _new_run(db, company_id, "INVENTORY")
    existing = (await repo.load_inventory(company_id, [key]))[key]
    next_day = replace(
        existing.record,
        inventory_date=existing.record.inventory_date + timedelta(days=1),
    )
    corrected = replace(
        existing.record, outbound_quantity=existing.record.outbound_quantity + 1
    )

    inserted = diff_inventory(None, next_day)
    updated = diff_inventory(existing, corrected)
    await repo.apply_inventory(company_id, run_id, [inserted.row, updated.row])
    await repo.insert_change_records(
        company_id, run_id, inserted.records + updated.records
    )

    keys = [key, (next_day.inventory_date, next_day.warehouse_id, next_day.sku_id)]
    loaded = await repo.load_inventory(company_id, keys)
    assert loaded[keys[0]].record == corrected
    assert loaded[keys[1]].record == next_day
    assert [r.change_type for r in await _records_of(db, run_id)] == [
        "INSERT",
        "UPDATE",
    ]


async def test_sku_and_warehouse_updates(db_write_session):
    db = db_write_session
    repo = IngestRepository(db)
    company_id = await _company(db)
    sku_code = await db.scalar(
        text("SELECT sku_code FROM skus WHERE company_id = :c LIMIT 1"),
        {"c": company_id},
    )
    warehouse_code = await db.scalar(
        text("SELECT warehouse_code FROM warehouses WHERE company_id = :c LIMIT 1"),
        {"c": company_id},
    )
    run_id = await _new_run(db, company_id, "PRODUCT")
    sku = (await repo.load_skus(company_id, [sku_code]))[sku_code]
    warehouse = (await repo.load_warehouses(company_id, [warehouse_code]))[
        warehouse_code
    ]

    sku_diff = diff_sku(
        sku, replace(sku.record, units_per_case=sku.record.units_per_case + 1)
    )
    warehouse_diff = diff_warehouse(
        warehouse, replace(warehouse.record, warehouse_name="Renamed")
    )
    await repo.apply_skus(company_id, [sku_diff.row])
    await repo.apply_warehouses(company_id, [warehouse_diff.row])
    await repo.insert_change_records(
        company_id, run_id, sku_diff.records + warehouse_diff.records
    )

    assert (await repo.load_skus(company_id, [sku_code]))[
        sku_code
    ].record == sku_diff.row.record
    assert (await repo.load_warehouses(company_id, [warehouse_code]))[
        warehouse_code
    ].record.warehouse_name == "Renamed"
    assert len(await _records_of(db, run_id)) == 2


async def test_next_applied_seq_is_max_plus_one(db_write_session):
    db = db_write_session
    repo = IngestRepository(db)
    company_id = await _company(db)
    current = await db.scalar(
        select(func.coalesce(func.max(DataSyncRun.applied_seq), 0)).where(
            DataSyncRun.company_id == company_id
        )
    )

    assert await repo.next_applied_seq(company_id) == current + 1
    assert await repo.next_applied_seq(uuid.uuid7()) == 1


# --- 수집 실행 기록 -----------------------------------------------------------------


async def _run_row(db, run_id: uuid.UUID):
    return (
        await db.execute(
            text(
                "SELECT status, received_record_count, loaded_record_count,"
                " rejected_record_count, applied_seq, finished_at"
                " FROM data_sync_runs WHERE data_sync_run_id = :r"
            ),
            {"r": run_id},
        )
    ).one()


async def test_run_starts_running_and_completes_with_counts(db_write_session):
    db = db_write_session
    repo = IngestRepository(db)
    company_id = await _company(db)
    run_id = await repo.start_run(
        company_id, SourceDomain.ORDER, ConnectionMethod.API, NOW
    )

    assert (await _run_row(db, run_id)).status == "RUNNING"

    seq = await repo.next_applied_seq(company_id)
    finished = NOW + timedelta(seconds=5)
    await repo.complete_run(company_id, run_id, RunCounts(10, 9, 1), seq, finished)

    assert tuple(await _run_row(db, run_id)) == (
        "PARTIAL_SUCCESS",
        10,
        9,
        1,
        seq,
        finished,
    )
    assert await repo.next_applied_seq(company_id) == seq + 1


async def test_failed_run_has_no_applied_seq(db_write_session):
    db = db_write_session
    repo = IngestRepository(db)
    company_id = await _company(db)
    run_id = await repo.start_run(
        company_id, SourceDomain.SHIPMENT, ConnectionMethod.FILE, NOW
    )

    await repo.fail_run(company_id, run_id, NOW + timedelta(seconds=1))

    row = await _run_row(db, run_id)
    assert (row.status, row.applied_seq, row.received_record_count) == (
        "FAILED",
        None,
        None,
    )


async def test_finished_run_cannot_be_finished_again(db_write_session):
    db = db_write_session
    repo = IngestRepository(db)
    company_id = await _company(db)
    run_id = await repo.start_run(
        company_id, SourceDomain.INVENTORY, ConnectionMethod.API, NOW
    )
    await repo.fail_run(company_id, run_id, NOW)

    with pytest.raises(LookupError):
        await repo.fail_run(company_id, run_id, NOW)
    with pytest.raises(LookupError):
        await repo.fail_run(uuid.uuid7(), run_id, NOW)  # 다른 고객사


# --- 외부 코드표 -------------------------------------------------------------------


async def test_all_shipments_rebuilt_from_tms_codes_match_confirmed_db(
    db_write_session,
):
    """
    2026 상반기 선적 166건(두 고객사)을 TMS 원천 모양(항구·기준·계획일 없이)으로 다시 만들어 새 선적으로 변환하면,
    경로표로 정한 항구, 시장 리드타임 기준, 계획 3PL 입고일이 확정 DB 와 모두 같다.
    """
    db = db_write_session
    repo = IngestRepository(db)
    companies = list(
        await db.scalars(text("SELECT DISTINCT company_id FROM shipments"))
    )
    if not companies:
        pytest.skip("선적 데이터가 없는 DB")
    ports = dict(
        (
            await db.execute(
                text(
                    "SELECT market_leadtime_reference_id, us_port_code"
                    " FROM market_leadtime_references"
                )
            )
        ).all()
    )

    checked, port_counts = 0, Counter()
    for company_id in companies:
        book = await repo.load_code_book(company_id, SourceDomain.SHIPMENT)
        tms_sku = {code.sku_id: external for external, code in book.skus.items()}
        tms_warehouse = {
            code.warehouse_id: external for external, code in book.warehouses.items()
        }
        codes = list(
            await db.scalars(
                text("SELECT shipment_code FROM shipments WHERE company_id = :c"),
                {"c": company_id},
            )
        )
        for shipment in (await repo.load_shipments(company_id, codes)).values():
            record = shipment.record
            source = SourceShipment(
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
            resolved = resolve_shipment(source, book)  # 새 선적처럼 기준·계획일을 계산
            assert resolved == record, record.shipment_code
            port_counts[ports[resolved.market_leadtime_reference_id]] += 1
            checked += 1

    total = await db.scalar(text("SELECT count(*) FROM shipments"))
    assert checked == total
    if total == 166:  # 확정 2026 상반기 데이터
        assert port_counts == {"2704": 83, "1003": 82, "2720": 1}


async def test_order_code_book_uses_internal_codes(db_write_session):
    db = db_write_session
    repo = IngestRepository(db)
    company_id = await _company(db)

    book = await repo.load_code_book(company_id, SourceDomain.ORDER)

    sku_codes = set(
        await db.scalars(
            text("SELECT sku_code FROM skus WHERE company_id = :c"), {"c": company_id}
        )
    )
    assert set(book.skus) == sku_codes
    assert all(code.sku_code == external for external, code in book.skus.items())
    assert book.leadtime_references == {}
    assert (await repo.load_code_book(uuid.uuid7(), SourceDomain.INVENTORY)).skus == {}


# --- 대량 키 (IN 조회 나누기) ---------------------------------------------------------


async def test_large_key_lists_are_split_and_still_find_real_rows(db_write_session):
    """키가 바인딩 한도(약 32,767)를 넘어도 나눠 조회해 실패하지 않고, 경계 뒤의 실제 행도 찾는다"""
    db = db_write_session
    repo = IngestRepository(db)
    company_id = await _company(db)
    real_orders = list(
        await db.scalars(
            text("SELECT order_code FROM orders WHERE company_id = :c LIMIT 3"),
            {"c": company_id},
        )
    )
    real_key = tuple(
        (
            await db.execute(
                text(
                    "SELECT inventory_date, warehouse_id, sku_id FROM inventory_daily"
                    " WHERE company_id = :c LIMIT 1"
                ),
                {"c": company_id},
            )
        ).one()
    )
    fake_codes = [f"NO-SUCH-{i}" for i in range(40_000)]
    fake_keys = [(real_key[0], uuid.uuid7(), uuid.uuid7()) for _ in range(12_000)]

    orders = await repo.load_orders(company_id, fake_codes + real_orders)
    inventory = await repo.load_inventory(company_id, fake_keys + [real_key])
    run_id = await _new_run(db, company_id, "ORDER")
    real_order_id = orders[real_orders[0]].row_id
    touched = [uuid.uuid7() for _ in range(40_000)] + [real_order_id]
    await repo._touch(
        Order, "order_id", company_id, touched, {"data_sync_run_id": run_id}
    )

    assert set(orders) == set(real_orders)
    assert set(inventory) == {real_key}
    assert (
        await db.scalar(
            text("SELECT data_sync_run_id FROM orders WHERE order_id = :o"),
            {"o": real_order_id},
        )
        == run_id
    )
