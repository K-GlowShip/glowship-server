import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.modules.data_sync.application.exception.data_sync_exceptions import (
    SyncInProgressException,
)
from app.modules.data_sync.application.ingest.ingest_service import (
    DataIngestService,
    StartedRun,
    whole_second,
)
from app.modules.data_sync.infrastructure.data_sync_enums import (
    ConnectionMethod,
    DataSyncRunStatus,
    SourceDomain,
)
from app.modules.data_sync.infrastructure.repository.ingest_repository import (
    IngestRepository,
)

"""
    수집 서비스 시작(start)·적용(apply)·실패 기록(fail) 나누기 (API 25 가 요청 안에서 start, 백그라운드에서 apply)
      - ingest = start + apply 라 기존 동작은 test_ingest_service_sql.py 가 그대로 검증합니다.
      - 여기서는 나눠 부를 때: start 가 RUNNING 을 커밋하고 실행 ID 를 주는지, 그 사이 같은 원천 시작이 409 인지,
        apply 가 그 실행을 완료하는지, 원천 응답을 받기 전에 실패하면 fail 로 FAILED 가 남아 다음 수집이 열리는지 본다.
      - 실제 DB, 끝나면 롤백 (db_write_session, SAVEPOINT 모드).
"""

STARTED_AT = datetime(2026, 7, 2, 0, 0, tzinfo=UTC)
SUB_SECOND = datetime(2026, 7, 2, 0, 3, 0, 987654, tzinfo=UTC)
WHOLE = datetime(2026, 7, 2, 0, 3, 0, tzinfo=UTC)


class NoStale:
    async def judge_after_sync(self, company_id, applied_seq, detected_at):
        return []


def _service(db) -> DataIngestService:
    return DataIngestService(
        db, IngestRepository(db), NoStale(), now=lambda: STARTED_AT
    )


async def _company(db) -> uuid.UUID:
    company_id = await db.scalar(text("SELECT company_id FROM shipments LIMIT 1"))
    if company_id is None:
        pytest.skip("선적 데이터가 없는 DB")
    return company_id


async def _run(db, run_id):
    return (
        await db.execute(
            text(
                "SELECT status, started_at, finished_at, applied_seq, received_record_count"
                " FROM data_sync_runs WHERE data_sync_run_id = :r"
            ),
            {"r": run_id},
        )
    ).one()


async def test_start_records_running_and_returns_run(db_write_session):
    db = db_write_session
    company_id = await _company(db)

    started = await _service(db).start(
        company_id, SourceDomain.SHIPMENT, ConnectionMethod.API
    )

    assert started == StartedRun(
        company_id=company_id,
        data_sync_run_id=started.data_sync_run_id,
        source_domain=SourceDomain.SHIPMENT,
        started_at=STARTED_AT,
    )
    run = await _run(db, started.data_sync_run_id)
    assert (run.status, run.started_at, run.finished_at, run.applied_seq) == (
        "RUNNING",
        STARTED_AT,
        None,
        None,
    )


async def test_second_start_of_same_source_is_rejected_until_applied(db_write_session):
    db = db_write_session
    company_id = await _company(db)
    service = _service(db)
    started = await service.start(
        company_id, SourceDomain.SHIPMENT, ConnectionMethod.API
    )

    with pytest.raises(SyncInProgressException):
        await service.start(company_id, SourceDomain.SHIPMENT, ConnectionMethod.API)
    # 다른 원천은 막지 않는다
    other = await service.start(company_id, SourceDomain.ORDER, ConnectionMethod.API)
    await service.apply(other, [])

    result = await service.apply(started, [])

    assert result.data_sync_run_id == started.data_sync_run_id
    assert result.status == DataSyncRunStatus.SUCCEEDED
    run = await _run(db, started.data_sync_run_id)
    assert (run.status, run.received_record_count) == ("SUCCEEDED", 0)
    assert run.applied_seq == result.applied_seq
    # 완료 뒤에는 다시 시작할 수 있다
    again = await service.start(company_id, SourceDomain.SHIPMENT, ConnectionMethod.API)
    assert again.data_sync_run_id != started.data_sync_run_id


async def test_fail_before_apply_marks_failed_and_reopens_source(db_write_session):
    """원천 응답 받기·파싱이 실패해 apply 를 못 부르면 fail 로 FAILED 를 남긴다 (RUNNING 으로 남으면 다음 수집이 계속 막힘)"""
    db = db_write_session
    company_id = await _company(db)
    service = _service(db)
    started = await service.start(
        company_id, SourceDomain.SHIPMENT, ConnectionMethod.API
    )
    error = OSError("가상 TMS 시나리오 파일을 읽지 못함")

    await service.fail(started, error)

    run = await _run(db, started.data_sync_run_id)
    assert (run.status, run.finished_at, run.applied_seq) == (
        "FAILED",
        STARTED_AT,
        None,
    )
    assert not getattr(error, "__notes__", [])
    reopened = await service.start(
        company_id, SourceDomain.SHIPMENT, ConnectionMethod.API
    )
    assert reopened.data_sync_run_id != started.data_sync_run_id


class RecordingStale:
    def __init__(self):
        self.detected_at = []

    async def judge_after_sync(self, company_id, applied_seq, detected_at):
        self.detected_at.append(detected_at)
        return []


async def test_times_drop_sub_second(db_write_session):
    """이슈 #39 규칙 1: 시계가 마이크로초를 줘도 시작·완료·STALE 판정 시각은 초 단위 (엔진 store.iso_ts)"""
    db = db_write_session
    company_id = await _company(db)
    stale = RecordingStale()
    service = DataIngestService(db, IngestRepository(db), stale, now=lambda: SUB_SECOND)

    started = await service.start(
        company_id, SourceDomain.SHIPMENT, ConnectionMethod.API
    )
    await service.apply(started, [])
    failed = await service.start(company_id, SourceDomain.ORDER, ConnectionMethod.API)
    await service.fail(failed, OSError("원천 호출 실패"))

    assert started.started_at == WHOLE
    run = await _run(db, started.data_sync_run_id)
    assert (run.started_at, run.finished_at) == (WHOLE, WHOLE)
    assert stale.detected_at == [WHOLE]
    failed_run = await _run(db, failed.data_sync_run_id)
    assert (failed_run.started_at, failed_run.finished_at) == (WHOLE, WHOLE)


def test_whole_second():
    assert whole_second(SUB_SECOND) == WHOLE
    assert whole_second(WHOLE) == WHOLE


async def test_fail_keeps_original_error_when_recording_fails(db_write_session):
    db = db_write_session
    company_id = await _company(db)
    service = _service(db)
    started = await service.start(
        company_id, SourceDomain.SHIPMENT, ConnectionMethod.API
    )
    await service.apply(started, [])  # 이미 RUNNING 이 아님 → FAILED 기록 실패
    error = OSError("원천 호출 실패")

    await service.fail(started, error)

    assert any("FAILED 기록도 실패" in note for note in error.__notes__)
    run = await _run(db, started.data_sync_run_id)
    assert run.status == "SUCCEEDED"
