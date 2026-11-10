import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from app.modules.delay_analysis.application.agent.fake_runtime import (
    FakeScenario,
    fake_runtime,
)
from app.modules.delay_analysis.application.agent.graph import (
    UNEXPECTED_ERROR_MESSAGE,
)
from app.modules.delay_analysis.application.agent.state import (
    AnalysisOutcome,
    AnalysisStep,
)
from app.modules.delay_analysis.application.run_start import runner as R
from app.modules.delay_analysis.application.run_start.runner import AnalysisRunner
from app.modules.delay_analysis.application.run_start.transitions import (
    RESTART_FAILURE_MESSAGE,
    UnfinishedRun,
)

"""
    분석 실행기 단위 시험 (GLOW-128, DB 없음)
      - 상태 전이·정리 조회·저장 함수(DB)는 가짜로 바꾸고, 그래프는 진짜(가짜 실행 묶음, fake_runtime)로 돌린다.
      - 확인: 꺼짐·중복 시작·동시 실행 수·단계 전이 순서·종료 상태별 저장·마지막 잡기·종료 취소·서버 시작 정리.
      - 실제 DB 로 하는 확인은 test_run_start_sql.py.
"""

COMPANY = uuid.UUID("00000000-0000-0000-0000-00000000c0de")


class _Run:
    def __init__(self, run_id: uuid.UUID):
        self.analysis_run_id = run_id
        self.company_id = COMPANY
        self.delay_event_id = uuid.uuid4()
        self.operating_rule_set_id = uuid.uuid4()
        self.input_seq = 1
        self.started_at = datetime(2026, 7, 2, 0, 0, tzinfo=UTC)


class _Session:
    """세션 가짜: 커밋·롤백 횟수만 센다."""

    def __init__(self, log):
        self.log = log

    async def commit(self):
        self.log.append("commit")

    async def rollback(self):
        self.log.append("rollback")

    async def get(self, _model, run_id):
        return _Run(run_id)


class _Db:
    """상태 전이·정리 조회·저장 가짜. 실제 DB 규칙(DETECTED 일 때만 시작 등)을 흉내 낸다."""

    def __init__(self):
        self.status: dict[uuid.UUID, str] = {}
        self.advanced: dict[uuid.UUID, list[AnalysisStep]] = {}
        self.saved: dict[uuid.UUID, dict] = {}
        self.failed: dict[uuid.UUID, str] = {}
        self.session_log: list[str] = []
        self.unfinished: list[UnfinishedRun] = []
        self.fail_raises = False
        self.fail_raises_for: set[uuid.UUID] = set()

    def detected(self, n=1):
        ids = [uuid.uuid4() for _ in range(n)]
        for i in ids:
            self.status[i] = "DETECTED"
        return ids

    @asynccontextmanager
    async def open_session(self):
        yield _Session(self.session_log)

    async def begin(self, db, *, company_id, analysis_run_id):
        if self.status.get(analysis_run_id) != "DETECTED":
            return None
        self.status[analysis_run_id] = "VALIDATING"
        return _Run(analysis_run_id)

    async def advance(self, db, *, company_id, analysis_run_id, to):
        self.advanced.setdefault(analysis_run_id, []).append(to)
        self.status[analysis_run_id] = to
        return True

    async def fail(self, db, *, company_id, analysis_run_id, message, now=None):
        if self.fail_raises or analysis_run_id in self.fail_raises_for:
            raise RuntimeError("저장 실패(시험)")
        self.failed[analysis_run_id] = message

    async def find(self, db):
        return self.unfinished

    async def save(self, db, run, state, *, now=None):
        self.saved[run.analysis_run_id] = {
            "outcome": state.get("outcome"),
            "now": now,
            "failure": state.get("failure"),
        }


class _Reader:
    def __init__(self, *_):
        pass

    async def get_status(self, _):
        return "VALIDATING"


async def _noop_rls(*_, **__):
    return None


@pytest.fixture
def db(monkeypatch):
    fake = _Db()
    monkeypatch.setattr(R, "begin_analysis", fake.begin)
    monkeypatch.setattr(R, "advance_status", fake.advance)
    monkeypatch.setattr(R, "fail_unfinished", fake.fail)
    monkeypatch.setattr(R, "find_unfinished_runs", fake.find)
    monkeypatch.setattr(R, "set_rls_context", _noop_rls)
    monkeypatch.setattr(R, "AnalysisRunStatusReader", _Reader)
    return fake


def _runner(db, scenario=None, **kw) -> AnalysisRunner:
    def factory(_session, _run):
        return fake_runtime(scenario)

    kw.setdefault("runtime_factory", factory)
    return AnalysisRunner(open_session=db.open_session, save_outcome=db.save, **kw)


async def test_disabled_runner_starts_nothing(db):
    (run_id,) = db.detected()
    in_progress = uuid.uuid4()
    db.status[in_progress] = "ANALYZING_IMPACT"
    db.unfinished = [
        UnfinishedRun(COMPANY, run_id, "DETECTED"),
        UnfinishedRun(COMPANY, in_progress, "ANALYZING_IMPACT"),
    ]
    runner = _runner(db, enabled=False)

    assert runner.start(COMPANY, run_id) is False
    assert runner.running == []
    assert (await runner.sweep_on_startup()).restarted == []
    await runner.wait_idle()
    assert db.status[run_id] == "DETECTED"
    assert db.failed == {}  # 꺼져 있으면 정리도 하지 않는다(DB 를 건드리지 않음)


@pytest.mark.parametrize(
    ("scenario", "outcome"),
    [
        (FakeScenario(), AnalysisOutcome.READY_FOR_REVIEW),
        (FakeScenario(no_delay=True), AnalysisOutcome.NO_DELAY),
        (FakeScenario(hold_code="REQUIRED_DATA_INVALID"), AnalysisOutcome.ON_HOLD),
        (
            FakeScenario(
                completed_status="NO_RISK",
                result_class="PURE_NO_RISK",
                has_at_risk_orders=False,
            ),
            AnalysisOutcome.NO_RISK,
        ),
        (FakeScenario(explanation_fails=True), AnalysisOutcome.FAILED),
    ],
)
async def test_each_outcome_is_saved_once(db, scenario, outcome):
    (run_id,) = db.detected()
    runner = _runner(db, scenario)

    assert runner.start(COMPANY, run_id) is True
    await runner.wait_idle()

    assert db.saved[run_id]["outcome"] == outcome
    assert run_id not in db.failed


async def test_ready_run_advances_status_in_order(db):
    (run_id,) = db.detected()
    runner = _runner(db)
    runner.start(COMPANY, run_id)
    await runner.wait_idle()

    assert db.advanced[run_id] == [
        AnalysisStep.ANALYZING_IMPACT,
        AnalysisStep.ANALYZING_OPTIONS,
        AnalysisStep.VALIDATING_OUTPUT,
    ]


async def test_no_delay_run_does_not_advance(db):
    (run_id,) = db.detected()
    runner = _runner(db, FakeScenario(no_delay=True))
    runner.start(COMPANY, run_id)
    await runner.wait_idle()

    assert run_id not in db.advanced  # 분석 준비에서 끝남: VALIDATING 그대로 저장


async def test_save_time_is_passed_to_save(db):
    (run_id,) = db.detected()
    at = datetime(2026, 7, 2, 0, 0, tzinfo=UTC)
    runner = _runner(db, save_time=lambda run: run.started_at)
    runner.start(COMPANY, run_id)
    await runner.wait_idle()

    assert db.saved[run_id]["now"] == at


async def test_same_run_is_started_once(db):
    (run_id,) = db.detected()
    calls = []

    def factory(_session, _run):
        calls.append(_run.analysis_run_id)
        return fake_runtime()

    runner = _runner(db, runtime_factory=factory)
    assert runner.start(COMPANY, run_id) is True
    assert runner.start(COMPANY, run_id) is False  # 돌고 있는 동안 두 번째는 무시
    await runner.wait_idle()
    assert runner.start(COMPANY, run_id) is True  # 끝난 뒤 다시 와도 시작 전이가 막음
    await runner.wait_idle()

    assert calls == [run_id]


async def test_concurrency_is_limited(db):
    ids = db.detected(5)
    live, peak = 0, 0
    gate = asyncio.Event()

    def factory(_session, _run):
        nonlocal live, peak
        runtime = fake_runtime()
        original = runtime.preparer.judge_no_delay

        async def slow():
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await gate.wait()
            live -= 1
            return await original()

        runtime.preparer.judge_no_delay = slow
        return runtime

    runner = _runner(db, runtime_factory=factory, max_concurrent_runs=2)
    for i in ids:
        runner.start(COMPANY, i)
    await asyncio.sleep(0.05)
    assert peak == 2
    assert (
        sum(1 for i in ids if db.status[i] == "DETECTED") == 3
    )  # 기다리는 실행은 DETECTED 그대로
    gate.set()
    await runner.wait_idle()

    assert set(db.saved) == set(ids)
    assert peak == 2


async def test_not_detected_run_is_not_analyzed(db):
    run_id = uuid.uuid4()
    db.status[run_id] = "STALE"
    runner = _runner(db)
    runner.start(COMPANY, run_id)
    await runner.wait_idle()

    assert run_id not in db.saved
    assert run_id not in db.failed


async def test_exception_outside_graph_is_saved_as_failed(db):
    (run_id,) = db.detected()

    def broken(_session, _run):
        raise RuntimeError("묶음을 만들다 실패(시험)")

    runner = _runner(db, runtime_factory=broken)
    runner.start(COMPANY, run_id)
    await runner.wait_idle()

    assert db.failed[run_id] == UNEXPECTED_ERROR_MESSAGE
    assert run_id not in db.saved


async def test_save_exception_is_caught_last(db):
    (run_id,) = db.detected()

    async def broken_save(*_, **__):
        raise RuntimeError("저장 실패(시험)")

    runner = AnalysisRunner(
        open_session=db.open_session,
        runtime_factory=lambda s, r: fake_runtime(),
        save_outcome=broken_save,
    )
    runner.start(COMPANY, run_id)
    await runner.wait_idle()

    assert db.failed[run_id] == UNEXPECTED_ERROR_MESSAGE


async def test_failed_last_save_only_logs_critical(db, caplog):
    (run_id,) = db.detected()
    db.fail_raises = True

    def broken(_session, _run):
        raise RuntimeError("시험")

    runner = _runner(db, runtime_factory=broken)
    with caplog.at_level(logging.CRITICAL):
        runner.start(COMPANY, run_id)
        await runner.wait_idle()

    assert any(r.levelno == logging.CRITICAL for r in caplog.records)
    assert runner.running == []  # 예외가 작업 밖으로 새지 않음


async def test_shutdown_cancels_after_grace_without_saving(db):
    (run_id,) = db.detected()
    never = asyncio.Event()

    def factory(_session, _run):
        runtime = fake_runtime()

        async def hang():
            await never.wait()

        runtime.preparer.judge_no_delay = hang
        return runtime

    runner = _runner(db, runtime_factory=factory, shutdown_grace_seconds=0.01)
    runner.start(COMPANY, run_id)
    await asyncio.sleep(0.02)
    await runner.shutdown()

    assert runner.running == []
    assert run_id not in db.saved
    assert run_id not in db.failed  # 취소는 잡지 않는다: 다음 시작 정리가 FAILED 로
    assert db.status[run_id] == "VALIDATING"


async def test_sweep_restarts_detected_and_fails_in_progress(db):
    (detected,) = db.detected()
    in_progress = uuid.uuid4()
    db.status[in_progress] = "ANALYZING_IMPACT"
    db.unfinished = [
        UnfinishedRun(COMPANY, detected, "DETECTED"),
        UnfinishedRun(COMPANY, in_progress, "ANALYZING_IMPACT"),
    ]
    runner = _runner(db)

    result = await runner.sweep_on_startup()
    await runner.wait_idle()

    assert result.restarted == [detected]
    assert db.failed[in_progress] == RESTART_FAILURE_MESSAGE
    assert db.saved[detected]["outcome"] == AnalysisOutcome.READY_FOR_REVIEW


async def test_sweep_continues_when_one_run_cannot_be_failed(db):
    """실행 하나를 FAILED 로 바꾸다 예외가 나도 서버 시작을 멈추지 않고 다음 실행을 정리한다."""
    broken, ok = uuid.uuid4(), uuid.uuid4()
    (detected,) = db.detected()
    for run_id in (broken, ok):
        db.status[run_id] = "ANALYZING_IMPACT"
    db.fail_raises_for = {broken}
    db.unfinished = [
        UnfinishedRun(COMPANY, broken, "ANALYZING_IMPACT"),
        UnfinishedRun(COMPANY, ok, "ANALYZING_IMPACT"),
        UnfinishedRun(COMPANY, detected, "DETECTED"),
    ]
    runner = _runner(db)

    result = await runner.sweep_on_startup()
    await runner.wait_idle()

    assert result.errors == [broken]
    assert broken not in db.failed
    assert db.failed[ok] == RESTART_FAILURE_MESSAGE
    assert result.restarted == [detected]
