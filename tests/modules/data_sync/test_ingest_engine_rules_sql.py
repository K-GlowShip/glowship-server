import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.modules.data_sync.application.ingest.ingest_service import DataIngestService
from app.modules.data_sync.infrastructure.data_sync_enums import (
    ConnectionMethod,
    SourceDomain,
)
from app.modules.data_sync.infrastructure.repository.ingest_repository import (
    IngestRepository,
)
from app.modules.delay_analysis.infrastructure.engine import engine_input_store as EIS

"""
    수집 시각이 엔진 규칙을 지키는지 (이슈 #39, PR #36 엔진 입력 저장소)
      - 규칙: 초 단위, 고객사 안에서 시작 시각이 겹치지 않음, 시작 시각 순서 = 적용 순번(applied_seq) 순서
      - 겹친 수집을 재현: 먼저 시작한(임시 시각이 이른) 수집이 늦게 적용된다. apply 가 시작 시각을 확정해
        두 번째로 적용된 수집의 시작 시각이 첫 번째보다 늦어지는지 본다.
      - 엔진 시험: 서버가 적재한 수집이 있는 DB 에서 load_company_engine_store 가 오류 없이 만들어지고
        check_input_seq 가 각 수집의 시작 시각 기준으로 통과하는지 (GLOWSHIP_ENGINE_DIR 이 있을 때만).
      - 실제 DB, 끝나면 롤백 (db_write_session).
"""

T0 = datetime(2026, 7, 2, 0, 3, tzinfo=UTC)
COMPANY = "B_COSMETICS"


class NoStale:
    async def judge_after_sync(self, company_id, applied_seq, detected_at):
        return []


def _service(db, at) -> DataIngestService:
    return DataIngestService(db, IngestRepository(db), NoStale(), now=lambda: at)


async def _company(db) -> uuid.UUID:
    company_id = await db.scalar(
        text("SELECT company_id FROM companies WHERE company_code = :c"),
        {"c": COMPANY},
    )
    if company_id is None:
        pytest.skip(f"{COMPANY} 가 없는 DB")
    return company_id


async def _ingest_overlapping(db, company_id):
    """SHIPMENT 가 T0 에 먼저 시작해 L1 을 기다리는 사이 ORDER 가 T0+1초에 시작해 먼저 적용된 경우"""
    shipment = await _service(db, T0).start(
        company_id, SourceDomain.SHIPMENT, ConnectionMethod.API
    )
    order = await _service(db, T0 + timedelta(seconds=1)).start(
        company_id, SourceDomain.ORDER, ConnectionMethod.API
    )
    first = await _service(db, T0 + timedelta(seconds=1)).apply(order, [])
    second = await _service(db, T0).apply(shipment, [])
    return shipment, order, first, second


async def _runs(db, company_id):
    return (
        await db.execute(
            text(
                "SELECT data_sync_run_id, started_at, finished_at, applied_seq FROM data_sync_runs"
                " WHERE company_id = :c ORDER BY started_at, data_sync_run_id"
            ),
            {"c": company_id},
        )
    ).all()


async def test_start_is_confirmed_after_last_applied_start(db_write_session):
    """이슈 #39 규칙 5: 먼저 시작했어도 늦게 적용된 수집은 시작 시각이 앞선 적용보다 늦다"""
    db = db_write_session
    company_id = await _company(db)

    shipment, order, first, second = await _ingest_overlapping(db, company_id)

    assert second.applied_seq == first.applied_seq + 1
    runs = {r.data_sync_run_id: r for r in await _runs(db, company_id)}
    assert runs[order.data_sync_run_id].started_at == T0 + timedelta(seconds=1)
    # 임시 T0 → 확정 T0+2초(앞선 적용 T0+1초 + 1초). 완료는 확정 시각 기준
    confirmed = runs[shipment.data_sync_run_id]
    assert confirmed.started_at == T0 + timedelta(seconds=2)
    assert confirmed.finished_at == T0 + timedelta(seconds=3)


async def test_company_runs_follow_engine_time_rules(db_write_session):
    """고객사의 모든 수집: 초 단위, 시작 초 겹침 없음, 적재된 수집은 시작 시각 순서 = 적용 순번 순서"""
    db = db_write_session
    company_id = await _company(db)
    await _ingest_overlapping(db, company_id)

    runs = await _runs(db, company_id)

    starts = [r.started_at for r in runs]
    assert all(s.microsecond == 0 for s in starts)
    assert len(set(starts)) == len(starts)
    applied = [r.applied_seq for r in runs if r.applied_seq is not None]
    assert applied == sorted(applied)
    assert all(r.finished_at > r.started_at for r in runs if r.finished_at)


async def test_engine_store_accepts_server_ingested_runs(db_write_session):
    """이슈 #39 시험: 서버가 적재한 수집이 있는 DB 에서 엔진 입력 저장소가 만들어지고 입력 순번 확인을 통과한다"""
    if get_settings().glowship_engine_dir is None:
        pytest.skip("GLOWSHIP_ENGINE_DIR 이 없어 실제 엔진 시험을 건너뜀")
    db = db_write_session
    company_id = await _company(db)
    _, _, first, second = await _ingest_overlapping(db, company_id)
    runs = {r.applied_seq: r for r in await _runs(db, company_id) if r.applied_seq}

    for seq in (first.applied_seq, second.applied_seq):
        store = await EIS.load_company_engine_store(db, company_id, seq)
        # 그 수집의 시작 시각에 입력을 확정한 실행이라고 보면 엔진이 보는 순번이 그 수집의 순번이어야 한다
        EIS.check_input_seq(
            store, company_id, EIS.engine_timestamp(runs[seq].started_at), seq
        )
