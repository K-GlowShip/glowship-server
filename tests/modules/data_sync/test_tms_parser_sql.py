from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.modules.data_sync.application.ingest.ingest_service import DataIngestService
from app.modules.data_sync.application.ingest.source_payload import (
    SourceShipment,
    SourceShipmentLine,
)
from app.modules.data_sync.application.ingest.tms_parser import parse_tms_shipments
from app.modules.data_sync.infrastructure.data_sync_enums import (
    ConnectionMethod,
    DataSyncRunStatus,
    SourceDomain,
)
from app.modules.data_sync.infrastructure.repository.ingest_repository import (
    IngestRepository,
)

"""
    TMS 응답 파서 확정 데이터 대조 (실제 DB, 쓰기는 끝나면 롤백)
      - 확정 선적 166건의 현재 값을 TMS 응답 JSON(외부 코드 = TMS 매핑 코드)으로 만들어 파싱하면,
        같은 값으로 직접 만든 SourceShipment 와 같고 거부가 없는지 본다.
      - 파싱 결과를 이름만 바꾼 새 선적(P-{원래 코드})으로 수집 서비스에 넣으면 거부 없이 SUCCEEDED 인지 본다.
"""

PREFIX = "P-"
OBSERVED_AT = datetime(2026, 7, 2, tzinfo=UTC)


class NoStale:
    async def judge_after_sync(self, company_id, applied_seq, detected_at):
        return []


def _iso(value):
    return value.isoformat() if value is not None else None


async def _confirmed(db, repo, company_id):
    """확정 선적의 (TMS 응답 JSON, 기대 SourceShipment) 목록"""
    book = await repo.load_code_book(company_id, SourceDomain.SHIPMENT)
    tms_sku = {code.sku_id: external for external, code in book.skus.items()}
    tms_warehouse = {
        c.warehouse_id: external for external, c in book.warehouses.items()
    }
    codes = list(
        await db.scalars(
            text("SELECT shipment_code FROM shipments WHERE company_id = :c"),
            {"c": company_id},
        )
    )
    pairs = []
    for loaded in (await repo.load_shipments(company_id, codes)).values():
        record = loaded.record
        lines = [
            (line.line_no, tms_sku[line.sku_id], line.shipped_quantity)
            for line in record.lines
        ]
        payload = {
            "shipmentNo": record.shipment_code,
            "mode": record.transport_mode,
            "destWh": tms_warehouse[record.warehouse_id],
            "plannedKrRelease": _iso(record.planned_kr_release_date),
            "stage": record.current_stage,
            "etaPort": _iso(record.current_expected_port_arrival_date),
            "eta3pl": _iso(record.current_expected_3pl_inbound_date),
            "actual3pl": _iso(record.actual_3pl_inbound_date),
            "items": [{"lineNo": n, "sku": s, "qty": q} for n, s, q in lines],
        }
        expected = SourceShipment(
            shipment_code=record.shipment_code,
            transport_mode=record.transport_mode,
            warehouse_code=tms_warehouse[record.warehouse_id],
            planned_kr_release_date=record.planned_kr_release_date,
            current_stage=record.current_stage,
            current_expected_port_arrival_date=record.current_expected_port_arrival_date,
            current_expected_3pl_inbound_date=record.current_expected_3pl_inbound_date,
            actual_3pl_inbound_date=record.actual_3pl_inbound_date,
            lines=tuple(SourceShipmentLine(n, s, q) for n, s, q in lines),
        )
        pairs.append((payload, expected))
    return pairs


async def _companies(db):
    companies = list(
        await db.scalars(text("SELECT DISTINCT company_id FROM shipments"))
    )
    if not companies:
        pytest.skip("선적 데이터가 없는 DB")
    return companies


async def test_confirmed_shipments_round_trip_through_tms_json(db_session):
    repo = IngestRepository(db_session)
    total = 0

    for company_id in await _companies(db_session):
        pairs = await _confirmed(db_session, repo, company_id)
        result = parse_tms_shipments([payload for payload, _ in pairs])

        assert result.rejections == []
        assert result.shipments == [expected for _, expected in pairs]
        total += len(pairs)

    assert total == await db_session.scalar(text("SELECT count(*) FROM shipments"))


async def test_parsed_shipments_load_without_rejection(db_write_session):
    db = db_write_session
    repo = IngestRepository(db)

    for company_id in await _companies(db):
        pairs = await _confirmed(db, repo, company_id)
        payloads = [
            payload | {"shipmentNo": PREFIX + payload["shipmentNo"]}
            for payload, _ in pairs
        ]
        parsed = parse_tms_shipments(payloads)
        service = DataIngestService(db, repo, NoStale(), now=lambda: OBSERVED_AT)

        result = await service.ingest(
            company_id,
            SourceDomain.SHIPMENT,
            ConnectionMethod.API,
            parsed.shipments,
            parsed.rejections,
        )

        assert result.status == DataSyncRunStatus.SUCCEEDED, result.rejections
        assert result.counts.loaded == len(payloads)
        assert result.rejections == ()
