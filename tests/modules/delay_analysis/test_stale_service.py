import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from app.modules.delay_analysis.application.stale import stale_service
from app.modules.delay_analysis.application.stale.stale_service import (
    StaleJudgmentService,
    mark_stale,
)
from app.modules.delay_analysis.infrastructure.analysis_run import AnalysisRun
from app.modules.delay_analysis.infrastructure.repository.stale_change_repository import (
    ScopedChange,
)

COMPANY_ID = uuid.uuid7()
DEST, DONOR = uuid.uuid7(), uuid.uuid7()
SKU = uuid.uuid7()
DELAYED_SHIPMENT = uuid.uuid7()
NOW = datetime(2026, 3, 6, tzinfo=UTC)

SCOPE = {
    "schema_version": "dependency_scope.v1",
    "base_date": "2026-03-01",
    "horizon": {"from": "2026-03-01", "to": "2026-03-31"},
    "order_scope": [{"warehouse_id": str(DEST), "sku_id": str(SKU)}],
    "inventory_scope": [],
    "warehouses": {"destination": str(DEST), "donor": str(DONOR)},
    "sku_sets": {"k_impacted": [str(SKU)], "k_watch": [], "k_shipment": [str(SKU)]},
    "entities": {"delayed_shipment_id": str(DELAYED_SHIPMENT)},
}


def _run(status: str = "READY_FOR_REVIEW", **kwargs) -> AnalysisRun:
    values = {
        "analysis_run_id": uuid.uuid7(),
        "company_id": COMPANY_ID,
        "delay_event_id": uuid.uuid7(),
        "input_seq": 10,
        "status": status,
        "is_latest": True,
        "dependency_scope": None if status == "DETECTED" else SCOPE,
        "started_at": datetime(2026, 3, 1, 0, 10, tzinfo=UTC),
    } | kwargs
    return AnalysisRun(**values)


def _order_change(seq: int, sku: uuid.UUID = SKU) -> ScopedChange:
    return ScopedChange(
        applied_seq=seq,
        sync_started_at=datetime(2026, 3, 5, tzinfo=UTC),
        sync_finished_at=datetime(2026, 3, 5, 0, 1, tzinfo=UTC),
        entity_type="ORDER",
        change_type="UPDATE",
        entity_ref=uuid.uuid7(),
        sku_id=sku,
        warehouse_id=DEST,
        changed_fields=(),
        change_summary="주문 수량 변경",
    )


# --- 가짜 리포지토리·세션·재계산기 -------------------------------------------------


@dataclass
class FakeChangeRepository:
    runs: list = field(default_factory=list)
    changes: list = field(default_factory=list)
    shipment_skus: tuple | None = None
    change_queries: list = field(default_factory=list)

    async def find_target_run_ids(self, company_id, applied_seq):
        return [run.analysis_run_id for run in self.runs]

    async def find_changes_after(self, company_id, after_seq, up_to_seq=None):
        self.change_queries.append((after_seq, up_to_seq))
        return [
            c
            for c in self.changes
            if c.applied_seq > after_seq
            and (up_to_seq is None or c.applied_seq <= up_to_seq)
        ]

    async def find_delayed_shipment_skus(self, company_id, delay_event_id):
        return self.shipment_skus


class FakeSession:
    flushed = 0

    async def flush(self):
        self.flushed += 1


@dataclass
class FakeRecalculator:
    changed: bool
    calls: list = field(default_factory=list)

    async def signature_changed(self, run, changes):
        self.calls.append([c.applied_seq for c in changes])
        return self.changed


@pytest.fixture
def locked(monkeypatch):
    """잠금 헬퍼를 가짜로 바꾸고, 잠근 실행 ID 를 기록합니다."""
    calls = []

    def install(runs):
        by_id = {run.analysis_run_id: run for run in runs}

        async def fake_lock_runs(session, run_ids):
            ids = sorted(set(run_ids))
            calls.append(ids)
            return [by_id[i] for i in ids if i in by_id]

        async def fake_lock_run(session, run_id):
            calls.append([run_id])
            return by_id.get(run_id)

        monkeypatch.setattr(stale_service, "lock_analysis_runs", fake_lock_runs)
        monkeypatch.setattr(stale_service, "lock_analysis_run", fake_lock_run)
        return calls

    return install


def _service(repo, recalculator=None) -> StaleJudgmentService:
    return StaleJudgmentService(FakeSession(), repo, recalculator)


# --- mark_stale -------------------------------------------------------------------


def test_mark_stale_single_reason():
    run = _run()

    mark_stale(run, ["RELEVANT_ORDER_CHANGED"], 13, NOW)

    assert run.status == "STALE"
    assert run.stale_primary_reason == "RELEVANT_ORDER_CHANGED"
    assert run.stale_reason_codes == ["RELEVANT_ORDER_CHANGED"]
    assert (run.stale_detected_seq, run.stale_detected_at) == (13, NOW)


def test_mark_stale_multiple_reasons_uses_multiple_primary():
    run = _run()

    mark_stale(run, ["RELEVANT_ORDER_CHANGED", "RELEVANT_INVENTORY_CHANGED"], 13, NOW)

    assert run.stale_primary_reason == "MULTIPLE_RELEVANT_CHANGES"
    assert run.stale_reason_codes == [
        "RELEVANT_INVENTORY_CHANGED",
        "RELEVANT_ORDER_CHANGED",
    ]


@pytest.mark.parametrize(
    ("codes", "seq"),
    [
        ([], 13),
        (["UNKNOWN"], 13),
        (["RELEVANT_ORDER_CHANGED"], None),
        (["TIME_ELAPSED"], 13),
    ],
)
def test_mark_stale_rejects_invalid_reason_or_seq(codes, seq):
    with pytest.raises(ValueError):
        mark_stale(_run(), codes, seq, NOW)


# --- judge_after_sync -------------------------------------------------------------


async def test_judge_after_sync_marks_run_with_relevant_change(locked):
    run = _run()
    locks = locked([run])
    repo = FakeChangeRepository(runs=[run], changes=[_order_change(11)])

    staled = await _service(repo).judge_after_sync(COMPANY_ID, 11, NOW)

    assert staled == [run]
    assert (run.status, run.stale_detected_seq) == ("STALE", 11)
    assert locks == [[run.analysis_run_id]]
    assert repo.change_queries == [(10, 11)]  # 방금 적용한 수집의 변경만 봅니다


async def test_judge_after_sync_keeps_run_when_change_is_outside_scope(locked):
    run = _run()
    locked([run])
    repo = FakeChangeRepository(
        runs=[run], changes=[_order_change(11, sku=uuid.uuid7())]
    )

    assert await _service(repo).judge_after_sync(COMPANY_ID, 11, NOW) == []
    assert run.status == "READY_FOR_REVIEW"


async def test_judge_after_sync_keeps_run_when_signature_is_unchanged(locked):
    run = _run()
    locked([run])
    repo = FakeChangeRepository(runs=[run], changes=[_order_change(11)])
    recalculator = FakeRecalculator(changed=False)

    assert (
        await _service(repo, recalculator).judge_after_sync(COMPANY_ID, 11, NOW) == []
    )
    assert run.status == "READY_FOR_REVIEW"
    assert recalculator.calls == [[11]]


async def test_judge_after_sync_skips_run_changed_after_lock(locked):
    finalized = _run(status="FINALIZED")
    superseded = _run(is_latest=False)
    locked([finalized, superseded])
    repo = FakeChangeRepository(
        runs=[finalized, superseded], changes=[_order_change(11)]
    )

    assert await _service(repo).judge_after_sync(COMPANY_ID, 11, NOW) == []
    assert finalized.status == "FINALIZED"
    assert superseded.status == "READY_FOR_REVIEW"


async def test_in_progress_run_uses_shipment_skus_without_recalculation(locked):
    run = _run(status="DETECTED")
    locked([run])
    repo = FakeChangeRepository(
        runs=[run], changes=[_order_change(11)], shipment_skus=(DELAYED_SHIPMENT, [SKU])
    )
    recalculator = FakeRecalculator(changed=False)

    staled = await _service(repo, recalculator).judge_after_sync(COMPANY_ID, 11, NOW)

    assert staled == [run]
    assert recalculator.calls == []


# --- recheck_run ------------------------------------------------------------------


async def test_recheck_run_stops_at_first_seq_whose_signature_changed(locked):
    run = _run()
    locked([run])
    repo = FakeChangeRepository(changes=[_order_change(11), _order_change(12)])

    @dataclass
    class ChangesAtSecondSync(FakeRecalculator):
        async def signature_changed(self, run, changes):
            self.calls.append([c.applied_seq for c in changes])
            return len(self.calls) == 2

    recalculator = ChangesAtSecondSync(changed=False)

    result = await _service(repo, recalculator).recheck_run(
        run.analysis_run_id, 12, NOW
    )

    assert result is run
    assert run.stale_detected_seq == 12
    assert recalculator.calls == [[11], [11, 12]]  # 앞선 수집까지 누적해 재계산합니다


# --- mark_runs_stale --------------------------------------------------------------


async def test_mark_runs_stale_skips_finalized_runs(locked):
    previous = _run(is_latest=False)
    finalized = _run(status="FINALIZED")
    locked([previous, finalized])
    repo = FakeChangeRepository()

    staled = await _service(repo).mark_runs_stale(
        [previous.analysis_run_id, finalized.analysis_run_id],
        "DELAY_SCHEDULE_CHANGED",
        15,
        NOW,
    )

    assert staled == [previous]
    assert previous.stale_primary_reason == "DELAY_SCHEDULE_CHANGED"
    assert finalized.status == "FINALIZED"
