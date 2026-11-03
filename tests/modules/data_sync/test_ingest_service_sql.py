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
    MISSING_REQUIRED,
    MIXED_HS10_IN_SHIPMENT,
    UNKNOWN_SKU,
    Rejection,
)
from app.modules.data_sync.application.ingest.ingest_service import (
    DUPLICATE_SOURCE_RECORD,
    DataIngestService,
)
from app.modules.data_sync.application.ingest.record_validation import (
    NEGATIVE_QUANTITY,
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


async def test_check_violation_rejects_one_record_without_failing_the_sync(
    db_write_session,
):
    """DB CHECK 를 어기는 레코드(order_lines_ck1 음수 수량)는 적용 전에 거부되고 나머지는 적재 (FAILED 아님)"""
    db = db_write_session
    company_id = await _company(db)
    good, other = await _source_orders(db, company_id, limit=2)
    line = other.lines[0]
    bad = replace(other, lines=(replace(line, ordered_quantity=-1), *other.lines[1:]))

    result = await _service(db).ingest(
        company_id, SourceDomain.ORDER, ConnectionMethod.FILE, [good, bad]
    )

    assert result.status == DataSyncRunStatus.PARTIAL_SUCCESS
    assert (result.counts.received, result.counts.loaded, result.counts.rejected) == (
        2,
        1,
        1,
    )
    assert [
        (r.source_record_key, r.field_name, r.error_code) for r in result.rejections
    ] == [(other.order_code, "lines[0].ordered_quantity", NEGATIVE_QUANTITY)]
    assert (await _run_row(db, result.data_sync_run_id)).status == "PARTIAL_SUCCESS"


PARSE_REJECTION = Rejection(
    "B-ORD-ERR-0003", "orderedAt", "INVALID_DATE", "주문 일시를 읽을 수 없음"
)


@pytest.mark.parametrize("good_count", [1, 0])
async def test_parse_rejections_are_counted_in_the_run(db_write_session, good_count):
    """파서가 거부한 레코드도 받은·거부 건수에 들어가 PARTIAL_SUCCESS (기준선 B사 주문 수집의 INVALID_DATE 처럼)"""
    db = db_write_session
    company_id = await _company(db)
    orders = (await _source_orders(db, company_id, limit=1))[:good_count]

    result = await _service(db).ingest(
        company_id,
        SourceDomain.ORDER,
        ConnectionMethod.FILE,
        orders,
        parse_rejections=[PARSE_REJECTION],
    )

    assert result.status == DataSyncRunStatus.PARTIAL_SUCCESS
    expected = (good_count + 1, good_count, 1)
    assert (
        result.counts.received,
        result.counts.loaded,
        result.counts.rejected,
    ) == expected
    assert result.rejections == (PARSE_REJECTION,)
    row = await _run_row(db, result.data_sync_run_id)
    assert (
        row.status,
        row.received_record_count,
        row.loaded_record_count,
        row.rejected_record_count,
    ) == ("PARTIAL_SUCCESS", *expected)


async def test_parse_rejections_come_before_apply_rejections(db_write_session):
    db = db_write_session
    company_id = await _company(db)
    [order] = await _source_orders(db, company_id, limit=1)
    bad = replace(
        order,
        order_code="TEST-BAD",
        lines=(SourceOrderLine(1, "NO-SUCH-SKU", 1, 0, "ACTIVE"),),
    )

    result = await _service(db).ingest(
        company_id,
        SourceDomain.ORDER,
        ConnectionMethod.FILE,
        [order, bad],
        parse_rejections=[PARSE_REJECTION],
    )

    assert (result.counts.received, result.counts.loaded, result.counts.rejected) == (
        3,
        1,
        2,
    )
    assert [r.error_code for r in result.rejections] == ["INVALID_DATE", UNKNOWN_SKU]


# --- 검증 이슈 기록 (GLOW-15) ------------------------------------------------------------


async def _issues(db, run_id):
    return (
        await db.execute(
            text(
                "SELECT source_entity_type, source_record_key, field_name, error_code, severity,"
                " blocks_analysis, detected_at, status, resolved_at FROM data_validation_issues"
                " WHERE data_sync_run_id = :r ORDER BY source_record_key"
            ),
            {"r": run_id},
        )
    ).all()


async def test_rejections_are_recorded_as_open_issues(db_write_session):
    """파서 거부·적재 단계 거부가 같은 수집 ID 의 검증 이슈 OPEN 으로 남음 (ERROR·분석 차단, 감지 시각 = 수집 시작)"""
    db = db_write_session
    company_id = await _company(db)
    [order] = await _source_orders(db, company_id, limit=1)
    bad = replace(
        order,
        order_code="TEST-BAD",
        lines=(SourceOrderLine(1, "NO-SUCH-SKU", 1, 0, "ACTIVE"),),
    )

    result = await _service(db).ingest(
        company_id,
        SourceDomain.ORDER,
        ConnectionMethod.FILE,
        [order, bad],
        parse_rejections=[PARSE_REJECTION],
    )

    issues = await _issues(db, result.data_sync_run_id)
    assert [
        (i.source_entity_type, i.source_record_key, i.field_name, i.error_code)
        for i in issues
    ] == [
        ("ORDER", "B-ORD-ERR-0003", "orderedAt", "INVALID_DATE"),
        ("ORDER", "TEST-BAD", "lines[0].sku_code", UNKNOWN_SKU),
    ]
    assert {
        (i.severity, i.blocks_analysis, i.status, i.resolved_at) for i in issues
    } == {("ERROR", True, "OPEN", None)}
    assert {i.detected_at for i in issues} == {NOW}
    assert len(issues) == result.counts.rejected


async def test_inventory_issue_keeps_baseline_key(db_write_session):
    """재고 이슈의 원천 유형은 INVENTORY, 키는 창고(외부)|SKU(내부)|일자 (기준선 행과 같은 모양)"""
    db = db_write_session
    row, record = await _mapped_inventory(db)
    negative = replace(record, on_hand_quantity=-1, available_quantity=-1)

    result = await _service(db).ingest(
        row.company_id, SourceDomain.INVENTORY, ConnectionMethod.FILE, [negative]
    )

    [issue] = await _issues(db, result.data_sync_run_id)
    assert (issue.source_entity_type, issue.source_record_key, issue.error_code) == (
        "INVENTORY",
        f"{row.wh}|{row.sku_code}|2026-07-01",
        NEGATIVE_QUANTITY,
    )


# --- 검증 이슈 해소 (GLOW-15) ------------------------------------------------------------

LATER = NOW + timedelta(days=1)


def _service_at(db, moment) -> DataIngestService:
    return DataIngestService(
        db, IngestRepository(db), RecordingStale(), now=lambda: moment
    )


async def _issue_states(db, company_id, key):
    return (
        await db.execute(
            text(
                "SELECT data_sync_run_id, source_entity_type, error_code, status, resolved_at"
                " FROM data_validation_issues WHERE company_id = :c AND source_record_key = :k"
                " ORDER BY detected_at, error_code"
            ),
            {"c": company_id, "k": key},
        )
    ).all()


async def test_later_successful_load_resolves_open_issue(db_write_session):
    """기준선 E9: 재고 거부(OPEN) → 뒤 수집에서 정정 적재 → RESOLVED, resolved_at = 뒤 수집 시작"""
    db = db_write_session
    row, record = await _mapped_inventory(db)
    key = f"{row.wh}|{row.sku_code}|2026-07-01"
    negative = replace(record, on_hand_quantity=-1, available_quantity=-1)

    first = await _service(db).ingest(
        row.company_id, SourceDomain.INVENTORY, ConnectionMethod.FILE, [negative]
    )
    second = await _service_at(db, LATER).ingest(
        row.company_id, SourceDomain.INVENTORY, ConnectionMethod.FILE, [record]
    )

    assert second.status == DataSyncRunStatus.SUCCEEDED
    assert [
        (i.data_sync_run_id, i.status, i.resolved_at)
        for i in await _issue_states(db, row.company_id, key)
    ] == [(first.data_sync_run_id, "RESOLVED", LATER)]


async def test_issue_stays_open_when_rejected_again_or_absent(db_write_session):
    """다시 거부된 키는 이전 이슈도 OPEN 이고 새 이슈가 하나 더 생김. 응답에 없던 키의 이슈도 OPEN (빠진 행은 정정이 아님)"""
    db = db_write_session
    row, record = await _mapped_inventory(db)
    again = replace(record, on_hand_quantity=-1, available_quantity=-1)
    absent = replace(again, inventory_date=record.inventory_date - timedelta(days=1))

    await _service(db).ingest(
        row.company_id, SourceDomain.INVENTORY, ConnectionMethod.FILE, [again, absent]
    )
    await _service_at(db, LATER).ingest(
        row.company_id, SourceDomain.INVENTORY, ConnectionMethod.FILE, [again]
    )

    again_key = f"{row.wh}|{row.sku_code}|2026-07-01"
    absent_key = f"{row.wh}|{row.sku_code}|2026-06-30"
    assert [i.status for i in await _issue_states(db, row.company_id, again_key)] == [
        "OPEN",
        "OPEN",
    ]
    assert [i.status for i in await _issue_states(db, row.company_id, absent_key)] == [
        "OPEN"
    ]


async def test_resolution_matches_source_type(db_write_session):
    """키가 같아도 원천 유형이 다르면 해소하지 않음 (선적 이슈 키 = 주문 코드여도 주문 적재로는 그대로)"""
    db = db_write_session
    company_id = await _company(db)
    [order] = await _source_orders(db, company_id, limit=1)
    shipment_issue = Rejection(order.order_code, "shipmentNo", "INVALID_VALUE", "시험")

    await _service(db).ingest(
        company_id,
        SourceDomain.SHIPMENT,
        ConnectionMethod.FILE,
        [],
        parse_rejections=[shipment_issue],
    )
    await _service_at(db, LATER).ingest(
        company_id, SourceDomain.ORDER, ConnectionMethod.FILE, [order]
    )

    assert [
        (i.source_entity_type, i.status)
        for i in await _issue_states(db, company_id, order.order_code)
    ] == [("SHIPMENT", "OPEN")]


async def test_first_loaded_duplicate_resolves_old_issue_and_records_new_one(
    db_write_session,
):
    """같은 키가 이번 수집에서 첫 건은 적재되고 두 번째는 중복으로 거부되면: 이전 이슈는 RESOLVED, 이번 중복 이슈는 OPEN"""
    db = db_write_session
    company_id = await _company(db)
    [order] = await _source_orders(db, company_id, limit=1)
    old_issue = Rejection(order.order_code, "orderedAt", "INVALID_DATE", "시험")

    await _service(db).ingest(
        company_id,
        SourceDomain.ORDER,
        ConnectionMethod.FILE,
        [],
        parse_rejections=[old_issue],
    )
    await _service_at(db, LATER).ingest(
        company_id, SourceDomain.ORDER, ConnectionMethod.FILE, [order, order]
    )

    assert [
        (i.error_code, i.status)
        for i in await _issue_states(db, company_id, order.order_code)
    ] == [("INVALID_DATE", "RESOLVED"), (DUPLICATE_SOURCE_RECORD, "OPEN")]


class FailingStale(RecordingStale):
    async def judge_after_sync(self, company_id, applied_seq, detected_at):
        raise RuntimeError("STALE 판정 실패")


async def test_issues_roll_back_with_failed_sync(db_write_session):
    """검증 이슈는 수집과 같은 트랜잭션이라, 뒤 단계가 실패해 FAILED 가 되면 이슈도 남지 않음"""
    db = db_write_session
    company_id = await _company(db)

    with pytest.raises(RuntimeError):
        await _service(db, stale=FailingStale()).ingest(
            company_id,
            SourceDomain.ORDER,
            ConnectionMethod.FILE,
            [],
            parse_rejections=[PARSE_REJECTION],
        )

    run_id = await db.scalar(
        text(
            "SELECT data_sync_run_id FROM data_sync_runs WHERE company_id = :c"
            " AND started_at = :t AND status = 'FAILED'"
        ),
        {"c": company_id, "t": NOW},
    )
    assert run_id is not None
    assert await _issues(db, run_id) == []


async def test_many_issues_are_all_recorded(db_write_session):
    """거부가 많아도 모두 기록됨 (4,500건 × 열 12개 = 54,000 값. 행 목록 INSERT 는 SQLAlchemy 가 나눠 실행해 바인딩 한도에 걸리지 않음)"""
    db = db_write_session
    company_id = await _company(db)
    rejections = [
        Rejection(f"TEST-PARSE-{n:05d}", "orderedAt", "INVALID_DATE", "읽을 수 없음")
        for n in range(4_500)
    ]

    result = await _service(db).ingest(
        company_id,
        SourceDomain.ORDER,
        ConnectionMethod.FILE,
        [],
        parse_rejections=rejections,
    )

    count = await db.scalar(
        text("SELECT count(*) FROM data_validation_issues WHERE data_sync_run_id = :r"),
        {"r": result.data_sync_run_id},
    )
    assert count == 4_500


async def _mapped_inventory(db):
    """실제 재고 매핑(창고·SKU)으로 원천 재고 1건을 만듭니다."""
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
    return row, record


async def test_duplicate_inventory_key_uses_baseline_format(db_write_session):
    """재고 중복 거부 키도 `창고 외부 코드|내부 SKU 코드|일자` (매핑된 창고도 외부 코드, 기준선 WMS-EWR01|SKU-215|2026-01-01)"""
    db = db_write_session
    row, record = await _mapped_inventory(db)

    result = await _service(db).ingest(
        row.company_id, SourceDomain.INVENTORY, ConnectionMethod.FILE, [record, record]
    )

    assert result.status == DataSyncRunStatus.PARTIAL_SUCCESS
    assert {(r.source_record_key, r.error_code) for r in result.rejections} == {
        (f"{row.wh}|{row.sku_code}|2026-07-01", DUPLICATE_SOURCE_RECORD)
    }


class AliasSkuRepository(IngestRepository):
    """코드표에 같은 내부 SKU 를 가리키는 외부 코드 ALIAS-SKU 를 하나 더 둡니다 (매핑 N → 1)."""

    def __init__(self, db, alias_of: str):
        super().__init__(db)
        self.alias_of = alias_of

    async def load_code_book(self, company_id, source_domain):
        book = await super().load_code_book(company_id, source_domain)
        return replace(book, skus={**book.skus, "ALIAS-SKU": book.skus[self.alias_of]})


async def test_external_codes_resolving_to_same_inventory_row_load_the_first(
    db_write_session,
):
    """외부 코드 2개가 같은 내부 (날짜, 창고, SKU)로 바뀌면 첫 레코드만 적재 (inventory_daily_uk2 위반으로 실패하지 않음)"""
    db = db_write_session
    row, record = await _mapped_inventory(db)
    alias = replace(
        record, sku_code="ALIAS-SKU", on_hand_quantity=99, available_quantity=99
    )

    result = await _service(db, repo=AliasSkuRepository(db, row.sku)).ingest(
        row.company_id, SourceDomain.INVENTORY, ConnectionMethod.FILE, [record, alias]
    )

    assert result.status == DataSyncRunStatus.PARTIAL_SUCCESS
    assert (result.counts.received, result.counts.loaded, result.counts.rejected) == (
        2,
        1,
        1,
    )
    assert [(r.source_record_key, r.error_code) for r in result.rejections] == [
        (f"{row.wh}|{row.sku_code}|2026-07-01", DUPLICATE_SOURCE_RECORD)
    ]
    on_hand = await db.scalar(
        text(
            "SELECT i.on_hand_quantity FROM inventory_daily i JOIN skus k USING (sku_id)"
            " WHERE i.company_id = :c AND k.sku_code = :s AND i.inventory_date = '2026-07-01'"
            " AND i.data_sync_run_id = :r"
        ),
        {"c": row.company_id, "s": row.sku_code, "r": result.data_sync_run_id},
    )
    assert on_hand == 10


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


class FailingTwiceRepository(FailingRepository):
    """적용도 실패하고 FAILED 기록도 실패합니다 (예: 연결 끊김)."""

    async def fail_run(self, company_id, data_sync_run_id, finished_at):
        raise LookupError("실패 기록 중 실패")


async def test_original_error_is_raised_when_recording_failure_fails(db_write_session):
    db = db_write_session
    company_id = await _company(db)
    [order] = await _source_orders(db, company_id, limit=1)
    service = _service(db, repo=FailingTwiceRepository(db))

    with pytest.raises(RuntimeError, match="적용 중 실패") as raised:
        await service.ingest(
            company_id,
            SourceDomain.ORDER,
            ConnectionMethod.API,
            [replace(order, status="SHIPPED")],
        )

    [note] = raised.value.__notes__
    assert "FAILED 기록도 실패" in note and "LookupError" in note


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


class OtherHs10Repository(IngestRepository):
    """코드표에 HS10 이 다른 SKU 코드 TMS-OTHER-HS 를 하나 더 둡니다 (확정 데이터는 HS10 이 하나뿐)."""

    async def load_code_book(self, company_id, source_domain):
        book = await super().load_code_book(company_id, source_domain)
        any_sku = next(iter(book.skus.values()))
        return replace(
            book,
            skus={**book.skus, "TMS-OTHER-HS": replace(any_sku, hs10="9999999999")},
        )


async def test_new_shipments_without_single_hs10_are_rejected_not_failed(
    db_write_session,
):
    """새 선적 혼합 HS10·빈 상세는 부모 1건씩 거부, 정상 새 선적은 적재 → PARTIAL_SUCCESS (FAILED 아님)"""
    db = db_write_session
    company_id = await _company(db)
    template = replace(
        await _tms_shipment(db, company_id),
        current_stage="AT_SEA",
        actual_3pl_inbound_date=None,
    )
    good, mixed, empty = (
        replace(template, shipment_code=f"TEST-{uuid.uuid7().hex[-10:]}")
        for _ in range(3)
    )
    next_line = max(line.line_no for line in template.lines) + 1
    mixed = replace(
        mixed,
        lines=(*template.lines, SourceShipmentLine(next_line, "TMS-OTHER-HS", 1)),
    )
    empty = replace(empty, lines=())

    result = await _service(db, repo=OtherHs10Repository(db)).ingest(
        company_id, SourceDomain.SHIPMENT, ConnectionMethod.API, [good, mixed, empty]
    )

    assert result.status == DataSyncRunStatus.PARTIAL_SUCCESS
    assert (result.counts.received, result.counts.loaded, result.counts.rejected) == (
        3,
        1,
        2,
    )
    assert {
        (r.source_record_key, r.field_name, r.error_code) for r in result.rejections
    } == {
        (mixed.shipment_code, "lines", MIXED_HS10_IN_SHIPMENT),
        (empty.shipment_code, "lines", MISSING_REQUIRED),
    }
    stored = set(
        await db.scalars(
            text("SELECT shipment_code FROM shipments WHERE shipment_code = ANY(:s)"),
            {"s": [good.shipment_code, mixed.shipment_code, empty.shipment_code]},
        )
    )
    assert stored == {good.shipment_code}


async def test_existing_shipment_without_lines_updates_schedule_and_keeps_lines(
    db_write_session,
):
    """기존 선적 + 빈 상세 + 일정 변경: 기존 상세는 그대로, 일정만 갱신 (응답에서 빠진 상세는 삭제로 보지 않음)"""
    db = db_write_session
    company_id = await _company(db)
    source = await _tms_shipment(db, company_id)
    lines_sql = text(
        "SELECT l.line_no, l.sku_id, l.shipped_quantity FROM shipment_lines l"
        " JOIN shipments h USING (shipment_id)"
        " WHERE h.shipment_code = :s AND h.company_id = :c ORDER BY l.line_no"
    )
    params = {"s": source.shipment_code, "c": company_id}
    lines_before = (await db.execute(lines_sql, params)).all()
    header_only = replace(
        source,
        current_expected_3pl_inbound_date=source.current_expected_3pl_inbound_date
        + timedelta(days=3),
        lines=(),
    )

    result = await _service(db).ingest(
        company_id, SourceDomain.SHIPMENT, ConnectionMethod.API, [header_only]
    )

    assert result.status == DataSyncRunStatus.SUCCEEDED, result.rejections
    assert (await db.execute(lines_sql, params)).all() == lines_before
    expected = await db.scalar(
        text(
            "SELECT current_expected_3pl_inbound_date FROM shipments"
            " WHERE shipment_code = :s AND company_id = :c"
        ),
        params,
    )
    assert expected == header_only.current_expected_3pl_inbound_date
    assert await _snapshot_count(db, result.data_sync_run_id) == 1
    # 머리 변경 기록은 기존 상세의 SKU 마다 남아 STALE 판정 범위가 줄지 않음
    assert result.change_record_count == len({line.sku_id for line in lines_before})


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
