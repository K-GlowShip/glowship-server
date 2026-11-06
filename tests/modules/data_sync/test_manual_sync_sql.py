from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.core.security.tenant import TenantContext
from app.modules.data_sync.application.exception.data_sync_exceptions import (
    SourceNotSupportedException,
    SyncInProgressException,
)
from app.modules.data_sync.application.manual_sync_service import ManualSyncService
from app.modules.data_sync.infrastructure.data_sync_enums import (
    DataSyncRunStatus,
    SourceDomain,
)
from app.modules.data_sync.infrastructure.repository.ingest_repository import (
    IngestRepository,
)
from app.modules.data_sync.infrastructure.virtual_tms import VirtualTmsScenario

"""
    수동 수집 (API 25) 서비스 통합 테스트 (실제 DB, 끝나면 롤백)
      - 확정 DB(마지막 SHIPMENT 수집 07-01)에서 첫 수동 수집은 07-02 00:00Z 이고, 가상 TMS 의 그날 응답을 적재한다.
      - 시험용 시나리오: A_BEAUTY 에 확정 선적과 같은 창고·SKU(TMS 코드)로 새 선적 1건, 예상 3PL 입고일을 크게 늦춰 지연.
        첫 관측이 지연이면 에피소드 1번이 열린다(앞선 SHIPMENT 수집이 있으므로 초기 적재 아님, PR #24).
        이 선적·날짜는 동작 확인용이며 데모 시나리오가 아니다.
      - 워커 세션 대신 시험 세션을 넘긴다 (세션을 닫지 않는 팩토리).
"""

START = datetime(2026, 7, 2, tzinfo=UTC)
# 첫 SHIPMENT 수동 수집 시각(원천 자리 00:03)과 완료(시작 + 30초), 이슈 #39 규칙 2·3
AT = datetime(2026, 7, 2, 0, 3, tzinfo=UTC)
FINISHED = datetime(2026, 7, 2, 0, 3, 30, tzinfo=UTC)
NEW_CODE = "V-TEST-0702-001"


async def _a_beauty(db):
    company_id = await db.scalar(
        text("SELECT company_id FROM companies WHERE company_code = 'A_BEAUTY'")
    )
    last = await db.scalar(
        text(
            "SELECT max(started_at) FROM data_sync_runs WHERE company_id = :c"
            " AND source_domain = 'SHIPMENT' AND status IN ('SUCCEEDED','PARTIAL_SUCCESS')"
        ),
        {"c": company_id},
    )
    if company_id is None or last is None or last.date().isoformat() != "2026-07-01":
        pytest.skip("확정 데이터(마지막 SHIPMENT 수집 07-01)가 아닌 DB")
    return company_id


async def _delayed_new_shipment(db, company_id) -> dict:
    """확정 선적 하나의 창고·SKU TMS 코드로 만든 새 지연 선적"""
    repo = IngestRepository(db)
    book = await repo.load_code_book(company_id, SourceDomain.SHIPMENT)
    tms_sku = {code.sku_id: external for external, code in book.skus.items()}
    tms_wh = {c.warehouse_id: external for external, c in book.warehouses.items()}
    code = await db.scalar(
        text(
            "SELECT shipment_code FROM shipments WHERE company_id = :c"
            " AND transport_mode = 'OCEAN' ORDER BY shipment_code LIMIT 1"
        ),
        {"c": company_id},
    )
    record = (await repo.load_shipments(company_id, [code]))[code].record
    return {
        "shipmentNo": NEW_CODE,
        "mode": "OCEAN",
        "destWh": tms_wh[record.warehouse_id],
        "plannedKrRelease": "2026-06-25",
        "stage": "AT_SEA",
        "etaPort": "2026-12-20",
        "eta3pl": "2026-12-31",
        "actual3pl": None,
        "items": [
            {
                "lineNo": line.line_no,
                "sku": tms_sku[line.sku_id],
                "qty": line.shipped_quantity,
            }
            for line in record.lines
        ],
    }


def _service(db, scenario_loader) -> ManualSyncService:
    @asynccontextmanager
    async def same_session():
        yield db

    return ManualSyncService(
        open_session=same_session, virtual_start=START, load_scenario=scenario_loader
    )


def _scenario(shipment: dict, fetched_at: str = "2026-07-02T00:00:00Z"):
    return VirtualTmsScenario.from_json(
        {
            "responses": [
                {
                    "fetched_at": fetched_at,
                    "_demo_company": "A_BEAUTY",
                    "data": [shipment],
                }
            ]
        }
    )


async def _run_row(db, run_id):
    return (
        await db.execute(
            text(
                "SELECT status, started_at, finished_at, loaded_record_count"
                " FROM data_sync_runs WHERE data_sync_run_id = :r"
            ),
            {"r": run_id},
        )
    ).one()


async def test_first_manual_sync_loads_july_2nd_and_opens_delay(db_write_session):
    db = db_write_session
    company_id = await _a_beauty(db)
    shipment = await _delayed_new_shipment(db, company_id)
    service = _service(db, lambda: _scenario(shipment))
    tenant = TenantContext(user_id=None, company_id=company_id)

    ticket = await service.request(tenant, SourceDomain.SHIPMENT)

    assert ticket.window.at == AT
    assert ticket.window.after.date().isoformat() == "2026-07-01"
    assert ticket.company_code == "A_BEAUTY"
    run = await _run_row(db, ticket.started.data_sync_run_id)
    assert (run.status, run.started_at) == ("RUNNING", AT)

    result = await service.run(ticket)

    assert result is not None and result.status == DataSyncRunStatus.SUCCEEDED
    run = await _run_row(db, ticket.started.data_sync_run_id)
    assert tuple(run) == ("SUCCEEDED", AT, FINISHED, 1)
    opened = (
        await db.execute(
            text(
                "SELECT e.episode_no, e.closed_at, e.detected_at FROM delay_events e"
                " JOIN shipments s ON s.shipment_id = e.shipment_id"
                " WHERE s.company_id = :c AND s.shipment_code = :code"
            ),
            {"c": company_id, "code": NEW_CODE},
        )
    ).one()
    # 열린 에피소드 = 판단 API 18~20 을 시연할 수 있는 지연
    assert tuple(opened) == (1, None, AT)

    # 다음 수동 수집은 하루 뒤, 같은 응답은 다시 받지 않는다
    again = await service.request(tenant, SourceDomain.SHIPMENT)
    assert again.window.at == datetime(2026, 7, 3, 0, 3, tzinfo=UTC)
    second = await service.run(again)
    assert second.counts.received == 0


async def test_only_shipment_is_supported(db_write_session):
    db = db_write_session
    company_id = await _a_beauty(db)
    service = _service(db, lambda: None)

    for domain in (d for d in SourceDomain if d != SourceDomain.SHIPMENT):
        with pytest.raises(SourceNotSupportedException):
            await service.request(
                TenantContext(user_id=None, company_id=company_id), domain
            )


async def test_second_request_while_running_is_409(db_write_session):
    db = db_write_session
    company_id = await _a_beauty(db)
    service = _service(db, lambda: None)
    tenant = TenantContext(user_id=None, company_id=company_id)
    await service.request(tenant, SourceDomain.SHIPMENT)

    with pytest.raises(SyncInProgressException):
        await service.request(tenant, SourceDomain.SHIPMENT)


async def test_no_scenario_makes_empty_successful_sync(db_write_session):
    db = db_write_session
    company_id = await _a_beauty(db)
    service = _service(db, lambda: None)
    ticket = await service.request(
        TenantContext(user_id=None, company_id=company_id), SourceDomain.SHIPMENT
    )

    result = await service.run(ticket)

    assert result.status == DataSyncRunStatus.SUCCEEDED
    assert result.counts.received == 0


async def test_scenario_failure_marks_failed_and_reopens(db_write_session, caplog):
    db = db_write_session
    company_id = await _a_beauty(db)

    def broken():
        raise OSError("시나리오 파일 없음")

    service = _service(db, broken)
    tenant = TenantContext(user_id=None, company_id=company_id)
    ticket = await service.request(tenant, SourceDomain.SHIPMENT)

    assert await service.run(ticket) is None

    run = await _run_row(db, ticket.started.data_sync_run_id)
    assert run.status == "FAILED"
    assert "원천 응답을 받지 못해 FAILED" in caplog.text
    # FAILED 는 적재된 수집이 아니므로 다시 누르면 같은 날(07-02)을 다시 시도한다
    retry = await service.request(tenant, SourceDomain.SHIPMENT)
    assert retry.window.at == AT
    # 같은 날 재시도는 FAILED 수집과 시작 초가 겹치지 않게 1초 뒤 (이슈 #39 규칙 4)
    assert retry.started.started_at == AT + timedelta(seconds=1)
