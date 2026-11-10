import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from app.modules.data_sync.application import manual_sync_service as M
from app.modules.data_sync.application.ingest.ingest_service import (
    IngestResult,
    StartedRun,
)
from app.modules.data_sync.application.manual_sync_service import (
    ManualSyncService,
    ManualSyncTicket,
)
from app.modules.data_sync.application.virtual_clock import SyncWindow
from app.modules.data_sync.infrastructure.data_sync_enums import (
    DataSyncRunStatus,
    SourceDomain,
)

"""
    수동 수집 뒤 AUTO 분석 실행 넘기기 (GLOW-128) — DB 없음
      - 적용이 커밋된 뒤 이 수집이 만든 실행(created_run_ids)을 start_runs 로 넘긴다.
      - 넘기는 함수가 예외를 내도 수집 결과는 그대로 돌려준다(이미 커밋됨). 적용이 실패하면 넘기지 않는다.
"""

COMPANY = uuid.uuid4()
CREATED = (uuid.uuid4(), uuid.uuid4())


@asynccontextmanager
async def _session():
    yield object()


class _Ingest:
    def __init__(self, result=None, error=None):
        self.result, self.error = result, error

    async def apply(self, *_):
        if self.error is not None:
            raise self.error
        return self.result

    async def fail(self, *_):
        return None


def _result(created):
    return IngestResult(
        data_sync_run_id=uuid.uuid4(),
        status=DataSyncRunStatus.SUCCEEDED,
        counts=None,
        applied_seq=7,
        change_record_count=0,
        rejections=(),
        staled_run_ids=(),
        created_run_ids=created,
    )


def _ticket():
    at = datetime(2026, 7, 2, tzinfo=UTC)
    return ManualSyncTicket(
        started=StartedRun(COMPANY, uuid.uuid4(), SourceDomain.SHIPMENT, at),
        window=SyncWindow(after=None, at=at),
        company_code="A",
    )


def _service(ingest, start_runs):
    service = ManualSyncService(
        open_session=_session,
        virtual_start=None,
        load_scenario=lambda: None,
        start_runs=start_runs,
    )
    service._ingest = lambda db, at: ingest
    return service


async def test_created_runs_are_handed_over_after_apply(monkeypatch):
    monkeypatch.setattr(M, "parse_tms_shipments", lambda _: _Parsed())
    handed = []
    service = _service(
        _Ingest(_result(CREATED)), lambda c, ids: handed.append((c, ids))
    )

    result = await service.run(_ticket())

    assert result.created_run_ids == CREATED
    assert handed == [(COMPANY, CREATED)]


async def test_no_created_runs_hands_nothing(monkeypatch):
    monkeypatch.setattr(M, "parse_tms_shipments", lambda _: _Parsed())
    handed = []
    service = _service(_Ingest(_result(())), lambda c, ids: handed.append(ids))

    await service.run(_ticket())

    assert handed == []


async def test_starter_error_keeps_sync_result(monkeypatch):
    monkeypatch.setattr(M, "parse_tms_shipments", lambda _: _Parsed())

    def broken(*_):
        raise RuntimeError("실행기 오류(시험)")

    service = _service(_Ingest(_result(CREATED)), broken)

    result = await service.run(_ticket())

    assert result is not None and result.created_run_ids == CREATED


async def test_failed_apply_hands_nothing(monkeypatch):
    monkeypatch.setattr(M, "parse_tms_shipments", lambda _: _Parsed())
    handed = []
    service = _service(
        _Ingest(error=RuntimeError("적용 실패(시험)")), lambda *a: handed.append(a)
    )

    assert await service.run(_ticket()) is None
    assert handed == []


class _Parsed:
    shipments = ()
    rejections = ()
