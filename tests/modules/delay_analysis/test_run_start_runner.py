import asyncio
import gc
import logging
import uuid
import weakref
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.modules.delay_analysis.application.agent.fake_runtime import (
    FakeScenario,
    fake_runtime,
)
from app.modules.delay_analysis.application.agent.graph import (
    UNEXPECTED_ERROR_MESSAGE,
)
from app.modules.delay_analysis.application.agent.retry import (
    RUN_TIME_LIMIT_EXCEEDED,
    RUN_TIME_LIMIT_MESSAGE,
)
from app.modules.delay_analysis.application.agent.runtime import RuntimeWiringError
from app.modules.delay_analysis.application.agent.state import (
    AnalysisOutcome,
    AnalysisStep,
)
from app.modules.delay_analysis.application.agent.tools.common import ToolName
from app.modules.delay_analysis.application.agent.tracing import RunTracer
from app.modules.delay_analysis.application.run_start import outcome_save
from app.modules.delay_analysis.application.run_start import runner as R
from app.modules.delay_analysis.application.run_start.runner import AnalysisRunner
from app.modules.delay_analysis.application.run_start.transitions import (
    RESTART_FAILURE_MESSAGE,
    RetireOutcome,
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
    """세션 가짜: 커밋·롤백 횟수만 센다. db.rollback_raises 면 되돌리기에서 예외(취소로 접속이 죽은 경우 흉내)."""

    def __init__(self, log, db=None):
        self.log = log
        self.db = db

    async def commit(self):
        self.log.append("commit")

    async def rollback(self):
        self.log.append("rollback")
        if self.db is not None and self.db.rollback_raises:
            raise RuntimeError("되돌리기 실패(시험)")

    async def get(self, _model, run_id):
        return _Run(run_id)


class _Db:
    """상태 전이·정리 조회·저장 가짜. 실제 DB 규칙(DETECTED 일 때만 시작 등)을 흉내 낸다."""

    def __init__(self):
        self.status: dict[uuid.UUID, str] = {}
        self.advanced: dict[uuid.UUID, list[AnalysisStep]] = {}
        self.saved: dict[uuid.UUID, dict] = {}
        self.failed: dict[uuid.UUID, str] = {}
        self.failed_code: dict[uuid.UUID, str | None] = {}
        # 시간 제한 시험: 오래 걸리게 할 시작 전이 실행·단계 전이 단계, 되돌리기 예외
        self.begin_stalls: set[uuid.UUID] = set()
        self.advance_stalls: set[AnalysisStep] = set()
        self.rollback_raises = False
        self.session_log: list[str] = []
        self.unfinished: list[UnfinishedRun] = []
        self.fail_raises = False
        self.fail_raises_for: set[uuid.UUID] = set()
        # 시작 정리의 닫힌 지연 처리 결과(없으면 NOT_TARGET = 평소대로 시작), 예외를 낼 실행
        self.retire_outcome: dict[uuid.UUID, RetireOutcome] = {}
        self.retire_raises_for: set[uuid.UUID] = set()

    def detected(self, n=1):
        ids = [uuid.uuid4() for _ in range(n)]
        for i in ids:
            self.status[i] = "DETECTED"
        return ids

    @asynccontextmanager
    async def open_session(self):
        yield _Session(self.session_log, self)

    async def begin(self, db, *, company_id, analysis_run_id):
        if analysis_run_id in self.begin_stalls:
            await asyncio.sleep(3600)
        if self.status.get(analysis_run_id) != "DETECTED":
            return None
        self.status[analysis_run_id] = "VALIDATING"
        return _Run(analysis_run_id)

    async def advance(self, db, *, company_id, analysis_run_id, to):
        if to in self.advance_stalls:
            await asyncio.sleep(3600)
        self.advanced.setdefault(analysis_run_id, []).append(to)
        self.status[analysis_run_id] = to
        return True

    async def fail(
        self, db, *, company_id, analysis_run_id, message, code=None, now=None
    ):
        if self.fail_raises or analysis_run_id in self.fail_raises_for:
            raise RuntimeError("저장 실패(시험)")
        if self.status.get(analysis_run_id) == "DETECTED":
            return  # 실제 fail_unfinished 처럼 시작 전 실행은 그대로
        self.failed[analysis_run_id] = message
        self.failed_code[analysis_run_id] = code

    async def find(self, db):
        return self.unfinished

    async def retire(self, db, *, company_id, analysis_run_id):
        if analysis_run_id in self.retire_raises_for:
            raise RuntimeError("지연 종료 확인 실패(시험)")
        outcome = self.retire_outcome.get(analysis_run_id, RetireOutcome.NOT_TARGET)
        if outcome == RetireOutcome.RETIRED:
            self.status[analysis_run_id] = "STALE"
        return outcome

    async def save(self, db, run, state, *, now=None, **options):
        self.saved[run.analysis_run_id] = {
            "outcome": state.get("outcome"),
            "now": now,
            "failure": state.get("failure"),
            "options": options,
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
    monkeypatch.setattr(R, "retire_unstarted_run_of_closed_episode", fake.retire)
    monkeypatch.setattr(R, "set_rls_context", _noop_rls)
    monkeypatch.setattr(R, "AnalysisRunStatusReader", _Reader)
    return fake


def _runner(db, scenario=None, **kw) -> AnalysisRunner:
    def factory(_session, _run, _tracer):
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

    def factory(_session, _run, _tracer):
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

    def factory(_session, _run, _tracer):
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

    def broken(_session, _run, _tracer):
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
        runtime_factory=lambda s, r, t: fake_runtime(),
        save_outcome=broken_save,
    )
    runner.start(COMPANY, run_id)
    await runner.wait_idle()

    assert db.failed[run_id] == UNEXPECTED_ERROR_MESSAGE


async def test_failed_last_save_only_logs_critical(db, caplog):
    (run_id,) = db.detected()
    db.fail_raises = True

    def broken(_session, _run, _tracer):
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

    def factory(_session, _run, _tracer):
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


# --- 추적(tracer_factory) -----------------------------------------------------------------


class _CountingTracer(RunTracer):
    """진짜 추적기(sink 없음, 사건은 events 에 쌓임). aclose 횟수를 센다. close_raises 면 닫기에서 예외."""

    def __init__(self, run, close_raises=False):
        super().__init__(run.analysis_run_id, run.company_id, [])
        self.closed = 0
        self.close_raises = close_raises

    async def aclose(self, timeout: float = 2.0) -> bool:
        self.closed += 1
        await super().aclose(timeout)
        if self.close_raises:
            raise RuntimeError("추적 닫기 실패(시험)")
        return True


def _traced_runner(db, made, *, close_raises=False, **kw):
    """실행마다 _CountingTracer 를 만들고, 실행 도구 묶음이 받은 추적기를 made 에 남긴다."""
    tracers = []

    def tracer_factory(run):
        tracer = _CountingTracer(run, close_raises=close_raises)
        tracers.append(tracer)
        return tracer

    def factory(_session, _run, tracer):
        made.append(tracer)
        return fake_runtime(tracer=tracer)

    kw.setdefault("runtime_factory", factory)
    return _runner(db, tracer_factory=tracer_factory, **kw), tracers


async def test_tracer_follows_one_run_through_tools_and_graph(db):
    """같은 추적기가 실행 도구 묶음(도구 시도)과 그래프 콜백(실행·노드 사건)에 함께 가고, 끝나면 aclose 로 닫힌다."""
    (run_id,) = db.detected()
    made = []
    runner, tracers = _traced_runner(db, made)

    runner.start(COMPANY, run_id)
    await runner.wait_idle()

    (tracer,) = tracers
    kinds = {e.kind for e in tracer.events}
    assert made == [tracer]
    assert {"RUN_START", "NODE_START", "TOOL_ATTEMPT", "RUN_END"} <= kinds
    assert {e.analysis_run_id for e in tracer.events} == {run_id}
    assert tracer.closed == 1
    assert db.saved[run_id]["outcome"] == AnalysisOutcome.READY_FOR_REVIEW


async def test_no_tracer_factory_means_no_tracing(db):
    (run_id,) = db.detected()
    made = []

    def factory(_session, _run, tracer):
        made.append(tracer)
        return fake_runtime()

    runner = _runner(db, runtime_factory=factory)
    runner.start(COMPANY, run_id)
    await runner.wait_idle()

    assert made == [None]
    assert db.saved[run_id]["outcome"] == AnalysisOutcome.READY_FOR_REVIEW


async def test_tracer_is_closed_when_graph_fails(db):
    """그래프 밖 예외로 끝나도 추적기는 닫히고, 실행은 마지막 잡기로 FAILED."""
    (run_id,) = db.detected()

    def broken(_session, _run, _tracer):
        raise RuntimeError("묶음 연결 실수(시험)")

    runner, tracers = _traced_runner(db, [], runtime_factory=broken)
    runner.start(COMPANY, run_id)
    await runner.wait_idle()

    assert tracers[0].closed == 1
    assert db.failed[run_id] == UNEXPECTED_ERROR_MESSAGE


async def test_tracer_factory_error_runs_without_tracing(db, caplog):
    """추적기를 만들다 예외가 나도 분석·저장은 그대로(agentflow 8.1 장애 영향)."""
    (run_id,) = db.detected()
    made = []

    def factory(_session, _run, tracer):
        made.append(tracer)
        return fake_runtime(tracer=tracer)

    def tracer_factory(_run):
        raise OSError("추적 폴더 없음(시험)")

    runner = _runner(db, runtime_factory=factory, tracer_factory=tracer_factory)
    with caplog.at_level(logging.WARNING):
        runner.start(COMPANY, run_id)
        await runner.wait_idle()

    assert made == [None]
    assert db.saved[run_id]["outcome"] == AnalysisOutcome.READY_FOR_REVIEW
    assert run_id not in db.failed
    assert "추적기를 만들지 못해" in caplog.text


async def test_tracer_close_error_keeps_saved_result(db, caplog):
    (run_id,) = db.detected()
    runner, tracers = _traced_runner(db, [], close_raises=True)

    with caplog.at_level(logging.WARNING):
        runner.start(COMPANY, run_id)
        await runner.wait_idle()

    assert tracers[0].closed == 1
    assert db.saved[run_id]["outcome"] == AnalysisOutcome.READY_FOR_REVIEW
    assert run_id not in db.failed
    assert "추적기를 닫지 못했습니다" in caplog.text


async def test_sweep_does_not_start_retired_or_unresolved_runs(db):
    """닫힌 지연이라 STALE 로 끝낸 실행·닫은 수집을 못 찾은 실행은 시작하지 않고, 나머지 DETECTED 는 시작한다."""
    retired, unknown, normal = db.detected(3)
    db.retire_outcome = {
        retired: RetireOutcome.RETIRED,
        unknown: RetireOutcome.CLOSE_SYNC_UNKNOWN,
    }
    db.unfinished = [
        UnfinishedRun(COMPANY, r, "DETECTED") for r in (retired, unknown, normal)
    ]
    runner = _runner(db)

    result = await runner.sweep_on_startup()
    await runner.wait_idle()

    assert (result.retired, result.left_detected, result.restarted) == (
        [retired],
        [unknown],
        [normal],
    )
    assert retired not in db.saved and unknown not in db.saved
    assert (db.status[retired], db.status[unknown]) == ("STALE", "DETECTED")
    assert db.saved[normal]["outcome"] == AnalysisOutcome.READY_FOR_REVIEW


async def test_sweep_commits_only_when_retired(db):
    """끝냈을 때만 커밋하고, 대상이 아니거나 못 찾았으면 되돌린다(잠금만 잡은 트랜잭션)."""
    retired, unknown = db.detected(2)
    db.retire_outcome = {
        retired: RetireOutcome.RETIRED,
        unknown: RetireOutcome.CLOSE_SYNC_UNKNOWN,
    }
    runner = _runner(db, enabled=True)

    db.session_log.clear()
    assert await runner._retire_if_closed(
        UnfinishedRun(COMPANY, retired, "DETECTED")
    ) == (RetireOutcome.RETIRED)
    assert db.session_log == ["commit"]
    db.session_log.clear()
    await runner._retire_if_closed(UnfinishedRun(COMPANY, unknown, "DETECTED"))
    assert db.session_log == ["rollback"]


async def test_sweep_continues_when_closed_check_raises(db):
    """지연 종료 확인이 예외를 내도 서버 시작을 멈추지 않고, 그 실행은 시작하지 않으며 다음 실행을 정리한다."""
    broken, ok = db.detected(2)
    db.retire_raises_for = {broken}
    db.unfinished = [
        UnfinishedRun(COMPANY, broken, "DETECTED"),
        UnfinishedRun(COMPANY, ok, "DETECTED"),
    ]
    runner = _runner(db)

    result = await runner.sweep_on_startup()
    await runner.wait_idle()

    assert result.errors == [broken] and result.restarted == [ok]
    assert db.status[broken] == "DETECTED" and broken not in db.saved


# --- 끝난 실행의 엔진 입력 놓기 (이슈 #123) -------------------------------------------------


class _HandOver:
    """엔진 입력을 한 번 넘기고 들고 있지 않는 분석 준비(실제 EnginePreparer 처럼 결과를 담아 두지 않음)."""

    def __init__(self, engine):
        self._engine = engine

    async def judge_no_delay(self):
        return None

    async def build_engine_input(self):
        engine, self._engine = self._engine, None
        return engine


def _pinning_runtime(made, *, decide_raises=None):
    """
    노드 안에서 긴 타이머를 만들고 취소하는 도구를 낀 가짜 묶음. asyncio 는 취소된 타이머를 예정 시각까지
    대기열에 두고, 타이머가 들고 있는 실행 문맥(그래프 설정 → 실행 묶음)도 그동안 남는다(asyncpg 가 노드 안에서
    거는 타이머와 같은 사슬). 엔진 입력은 약한 참조로만 밖에 남긴다.
    """

    def factory(_session, _run, _tracer):
        runtime = fake_runtime()
        engine = runtime.preparer.engine
        made["engine"] = weakref.ref(engine)
        runtime.preparer = _HandOver(engine)
        del engine
        spec = runtime.registry[ToolName.GET_DELAY_CONTEXT]
        original = spec.fn

        async def pinning(ctx, payload):
            asyncio.get_running_loop().call_later(3600, lambda: None).cancel()
            return await original(ctx, payload)

        runtime.registry = {
            **runtime.registry,
            ToolName.GET_DELAY_CONTEXT: replace(spec, fn=pinning),
        }
        if decide_raises is not None:

            def broken_decide(engine):
                # 예외의 traceback 이 이 함수의 지역 변수를 붙잡으므로(pytest 가 로그 기록을 보관) 인자를 먼저 놓는다
                del engine
                raise decide_raises

            runtime.decide = broken_decide
        made["runtime"] = runtime
        return runtime

    return factory


async def test_finished_run_releases_engine_input_held_by_its_context(db):
    """
    실행 문맥이 남아도(취소된 타이머) 끝난 실행의 엔진 입력은 풀린다. 놓지 않으면 엔진 저장소(0.3~0.5GB)가
    타이머 예정 시각까지 남아, 순번이 다른 실행이 이어지면 2GB 서버가 메모리 부족으로 죽는다(이슈 #123).
    """
    (run_id,) = db.detected()
    made = {}
    runner = _runner(db, runtime_factory=_pinning_runtime(made))

    runner.start(COMPANY, run_id)
    await runner.wait_idle()
    gc.collect()

    assert db.saved[run_id]["outcome"] == AnalysisOutcome.READY_FOR_REVIEW
    assert made["engine"]() is None
    with pytest.raises(RuntimeWiringError, match="놓은"):
        made["runtime"].ctx  # noqa: B018 — 놓은 묶음을 다시 쓰면 연결 실수로 알림


async def test_engine_input_is_released_when_graph_fails(db):
    """그래프 밖으로 나온 예외(판정에서 연결 실수)로 끝나도 놓는다. 그 실행은 마지막 잡기로 FAILED."""
    (run_id,) = db.detected()
    made = {}
    runner = _runner(
        db,
        runtime_factory=_pinning_runtime(
            made, decide_raises=RuntimeWiringError("연결 실수(시험)")
        ),
    )

    runner.start(COMPANY, run_id)
    await runner.wait_idle()
    gc.collect()

    assert db.failed[run_id] == UNEXPECTED_ERROR_MESSAGE
    assert made["engine"]() is None


def test_released_runtime_cannot_start_again():
    """놓은 묶음으로 다시 분석을 시작하면 연결 실수(묶음 하나 = 실행 하나는 그대로)."""
    runtime = fake_runtime()
    runtime.start(
        company_id=COMPANY, analysis_run_id=uuid.uuid4(), engine=runtime.preparer.engine
    )
    runtime.release()

    with pytest.raises(RuntimeWiringError, match="놓은"):
        runtime.start(
            company_id=COMPANY,
            analysis_run_id=uuid.uuid4(),
            engine=runtime.preparer.engine,
        )


# --- 시간 제한 (이슈 #123) ------------------------------------------------------------------

LIMIT = 0.2  # 시험용 제한(초). 멈춘 도구·저장은 3600초를 기다리므로 제한이 없으면 시험이 끝나지 않는다


def _stalling_runtime(made, stall_in=frozenset()):
    """stall_in 에 든 실행은 T1 도구에서 멈춘다(취소될 때까지). 만든 묶음은 made 에 남긴다."""

    def factory(_session, run, _tracer):
        runtime = fake_runtime()
        if run.analysis_run_id in stall_in:
            spec = runtime.registry[ToolName.GET_DELAY_CONTEXT]

            async def stall(ctx, payload):
                await asyncio.sleep(3600)

            runtime.registry = {
                **runtime.registry,
                ToolName.GET_DELAY_CONTEXT: replace(spec, fn=stall),
            }
        made.append(runtime)
        return runtime

    return factory


async def _idle(runner):
    async with asyncio.timeout(10):  # 제한이 안 걸리면 여기서 실패(시험이 멈추지 않게)
        await runner.wait_idle()


async def test_run_over_analysis_limit_is_failed_and_frees_the_slot(db):
    """분석 제한을 넘은 실행은 지금 단계 FAILED(RUN_TIME_LIMIT_EXCEEDED). 자리가 풀려 기다리던 실행이 끝까지 돈다."""
    stuck, waiting = db.detected(2)
    made = []
    runner = _runner(
        db,
        runtime_factory=_stalling_runtime(made, {stuck}),
        max_concurrent_runs=1,
        run_time_limit_seconds=LIMIT,
    )

    runner.start(COMPANY, stuck)
    runner.start(COMPANY, waiting)
    await _idle(runner)

    assert (db.failed[stuck], db.failed_code[stuck]) == (
        RUN_TIME_LIMIT_MESSAGE,
        RUN_TIME_LIMIT_EXCEEDED,
    )
    assert stuck not in db.saved
    assert db.saved[waiting]["outcome"] == AnalysisOutcome.READY_FOR_REVIEW
    with pytest.raises(RuntimeWiringError, match="놓은"):
        made[0].ctx  # noqa: B018 — 제한으로 끝나도 엔진 입력을 놓음


async def test_limited_run_closes_its_tracer(db):
    (run_id,) = db.detected()
    made = []
    runner, tracers = _traced_runner(
        db,
        [],
        runtime_factory=_stalling_runtime(made, {run_id}),
        run_time_limit_seconds=LIMIT,
    )

    runner.start(COMPANY, run_id)
    await _idle(runner)

    assert tracers[0].closed == 1
    assert db.failed_code[run_id] == RUN_TIME_LIMIT_EXCEEDED


async def test_limit_is_judged_by_expiry_not_by_exception_type(db):
    """취소 뒤 그래프 세션 되돌리기가 다른 예외를 내도 제한으로 처리한다(마지막 잡기의 '예상하지 못한 오류'가 아님)."""
    (run_id,) = db.detected()
    made = []

    def factory(session, run, tracer):
        db.rollback_raises = True  # 그래프 세션을 되돌릴 때 예외
        return _stalling_runtime(made, {run_id})(session, run, tracer)

    runner = _runner(db, runtime_factory=factory, run_time_limit_seconds=LIMIT)
    runner.start(COMPANY, run_id)
    await _idle(runner)

    assert db.failed_code[run_id] == RUN_TIME_LIMIT_EXCEEDED


async def test_graph_is_closed_before_its_session_is_rolled_back(db):
    """단계 전이 도중 제한에 걸려도 그래프 스트림을 먼저 닫고 그래프 세션을 되돌린다(닫힌 세션을 쓰는 노드가 남지 않게)."""
    (run_id,) = db.detected()
    db.advance_stalls = {AnalysisStep.ANALYZING_IMPACT}
    log = db.session_log

    class _Graph:
        async def astream(self, state, config, stream_mode):
            try:
                yield {"step": AnalysisStep.ANALYZING_IMPACT}
                yield {"outcome": AnalysisOutcome.NO_DELAY}
            finally:
                log.append("graph_closed")

    runner = _runner(db, graph=_Graph(), run_time_limit_seconds=LIMIT)
    runner.start(COMPANY, run_id)
    await _idle(runner)

    closed = log.index("graph_closed")
    assert log[closed + 1] == "rollback"
    assert db.failed_code[run_id] == RUN_TIME_LIMIT_EXCEEDED


async def test_limit_during_start_transition_leaves_run_detected(db):
    """시작 전이에서 넘으면 실행은 DETECTED 그대로(FAILED 로 안 바꿈, 다음 시작 정리가 다시 시작). 자리는 풀린다."""
    stuck, waiting = db.detected(2)
    db.begin_stalls = {stuck}
    runner = _runner(db, max_concurrent_runs=1, run_time_limit_seconds=LIMIT)

    runner.start(COMPANY, stuck)
    runner.start(COMPANY, waiting)
    await _idle(runner)

    assert db.status[stuck] == "DETECTED"
    assert stuck not in db.failed
    assert db.saved[waiting]["outcome"] == AnalysisOutcome.READY_FOR_REVIEW


async def test_shutdown_cancel_is_not_turned_into_limit_failure(db):
    """서버 종료의 취소는 제한 FAILED 가 아니다(진행 중으로 남아 다음 시작 정리가 재시작 문구로 끝냄)."""
    (run_id,) = db.detected()
    runner = _runner(
        db,
        runtime_factory=_stalling_runtime([], {run_id}),
        run_time_limit_seconds=30,
        shutdown_grace_seconds=0.01,
    )
    runner.start(COMPANY, run_id)
    await asyncio.sleep(0.05)
    await runner.shutdown()

    assert runner.running == []
    assert run_id not in db.failed


def _slow_save(db, *, fast_retry=True):
    """처음 저장(재계산 포함)은 멈추고, 재계산 없이 다시 저장(recalculate=False)은 fast_retry 면 바로 끝난다."""

    async def save(session, run, state, *, now=None, **options):
        if options.get("recalculate") is False and fast_retry:
            return await db.save(session, run, state, now=now, **options)
        await asyncio.sleep(3600)

    return save


async def test_save_over_limit_is_saved_again_without_recalculation(db):
    (run_id,) = db.detected()
    runner = AnalysisRunner(
        open_session=db.open_session,
        runtime_factory=lambda s, r, t: fake_runtime(),
        save_outcome=_slow_save(db),
        save_time_limit_seconds=LIMIT,
    )
    runner.start(COMPANY, run_id)
    await _idle(runner)

    assert db.saved[run_id]["outcome"] == AnalysisOutcome.READY_FOR_REVIEW
    assert db.saved[run_id]["options"] == {"recalculate": False}
    assert run_id not in db.failed


async def test_save_again_over_cleanup_limit_is_failed(db, monkeypatch):
    monkeypatch.setattr(R, "CLEANUP_LIMIT_SECONDS", LIMIT)
    (run_id,) = db.detected()
    runner = AnalysisRunner(
        open_session=db.open_session,
        runtime_factory=lambda s, r, t: fake_runtime(),
        save_outcome=_slow_save(db, fast_retry=False),
        save_time_limit_seconds=LIMIT,
    )
    runner.start(COMPANY, run_id)
    await _idle(runner)

    assert db.failed_code[run_id] == RUN_TIME_LIMIT_EXCEEDED
    assert run_id not in db.saved


async def test_first_save_does_not_pass_save_options(db):
    """처음 저장은 지금처럼 부른다(저장 함수를 바꿔 끼운 시험·장치가 그대로 동작)."""
    (run_id,) = db.detected()
    runner = _runner(db, run_time_limit_seconds=30, save_time_limit_seconds=30)
    runner.start(COMPANY, run_id)
    await _idle(runner)

    assert db.saved[run_id]["options"] == {}


async def test_save_without_recalculation_passes_no_recalculator(monkeypatch):
    captured = []

    async def no_versions(*_):
        return None

    async def save_result(db, **kwargs):
        captured.append(kwargs["recalculator"])

    monkeypatch.setattr(outcome_save, "record_input_data_versions", no_versions)
    monkeypatch.setattr(
        outcome_save, "analysis_result_from_state", lambda *_, **__: None
    )
    monkeypatch.setattr(outcome_save, "save_analysis_result", save_result)
    monkeypatch.setattr(outcome_save, "recalculator_or_none", lambda db: "재계산기")
    state = {"outcome": AnalysisOutcome.READY_FOR_REVIEW}

    await outcome_save.save_graph_outcome(None, _Run(uuid.uuid4()), state)
    await outcome_save.save_graph_outcome(
        None, _Run(uuid.uuid4()), state, recalculate=False
    )

    assert captured == ["재계산기", None]
