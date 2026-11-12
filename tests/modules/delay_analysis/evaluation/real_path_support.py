import asyncio
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import MOCK_EXPLANATION_MODEL, get_settings
from app.modules.delay_analysis.application.agent.explanation import make_explainer
from app.modules.delay_analysis.application.agent.retry import (
    EXPLANATION_GENERATION_FAILED,
    TOOL_TIMEOUT_EXHAUSTED,
)
from app.modules.delay_analysis.application.agent.runtime import AnalysisRuntime
from app.modules.delay_analysis.application.agent.tools.common import ToolName
from app.modules.delay_analysis.application.agent.tools.registry import ToolRegistry
from app.modules.delay_analysis.application.run_start.engine_runtime import (
    build_engine_runtime,
)
from app.modules.delay_analysis.application.run_start.outcome_save import (
    save_graph_outcome,
)
from app.modules.delay_analysis.application.run_start.runner import (
    AnalysisRunner,
    TracerFactory,
)
from app.modules.delay_analysis.application.run_start.transitions import StartedRun
from app.modules.delay_analysis.infrastructure.engine.engine_loader import (
    load_engine_module,
)
from app.modules.delay_analysis.infrastructure.engine.explanation_assets import (
    load_explanation_assets,
)
from tests.modules.delay_analysis.test_explanation_runtime import (
    FAULTS as RT2_FAULTS,
)
from tests.modules.delay_analysis.test_result_save import _clear, _snapshot

"""
    실제 경로 시험 장치 (GLOW-58 2단계, docs/analysis/evaluation-scenarios.md 2단계 절). 시험 파일이 아니다.
      - 실제 경로 = 앱 실행기(app_runner.build_app_runner)가 가상 시각으로 만드는 것과 같은 조립:
        build_engine_runtime(실제 도구 12개·분석 준비·판정·설명) → AnalysisRunner → save_graph_outcome.
        설명 생성 시각 = 실행 시작 시각, 저장 끝난 시각 = 시작 + 1초(이슈 #50 결정). 다른 것은 세션뿐이다:
        앱은 워커 접속을 새로 열고, 여기서는 롤백 세션 하나를 실행기에 돌려주고 SET LOCAL ROLE 로 워커 역할을 건다.
        실행기가 세션을 열 때마다 RLS 문맥(app.user_id·app.company_id)을 비워, 새 접속에서는 문맥이 비어 있는 것을
        흉내 낸다(실행기가 문맥을 넣지 않으면 워커 역할에서 행이 보이지 않아 시험이 실패한다).
      - 한계: 조립은 build_app_runner 를 부르지 않고 같은 내용을 따라 적은 것이다. app_runner 의 조립이 바뀌면 이 장치도
        함께 고쳐야 한다(app_runner 자체는 test_run_start_app_runner 가 본다).
      - 설명 모델 설정(EXPLANATION_MODEL)이 mock 이 아니면 건너뛴다. 정답은 엔진 템플릿 설명으로 만들어졌고,
        시험이 실제 모델을 부르지 않게 하기 위해서다.
      - 확정 DB(정답 overlay)의 실행은 모두 끝난 상태라, 실행 하나를 같은 트랜잭션 안에서 DETECTED 로 되돌려 다시 돌린다.
        되돌리는 순서: 실패 요약 비우기 → 결과 행·초안·최종 선택 지우기 → DETECTED. 이 순서가 아니면 ck10 에 걸린다.
      - 장애 주입(정답 RT-1·RT-2 를 만든 주입과 같음): RT-1 = T5 가 시도별 시간 제한을 넘김, RT-2 = 설명 결함 3개.
      - 비교: 되돌리기 전 행(정답)과 저장 뒤 행을 열 단위로 견준다. 실패 문구(정답은 엔진 하네스의 기술 기록)와
        상태·STALE 열(정답의 STALE·FINALIZED 는 분석 뒤의 일)은 따로 본다.
      - 시험은 끝나면 롤백한다(tests/conftest.py db_write_session). 엔진 경로나 정답 실행이 없으면 건너뛴다.
"""

WORKER_ROLE = "glowship_v4_worker"
# 새로 연 세션의 RLS 문맥(빈 값). app/core/rls_context.set_rls_context 와 같은 설정 이름·트랜잭션 범위
_EMPTY_RLS_CONTEXT = text(
    "SELECT set_config('app.user_id', '', true), set_config('app.company_id', '', true)"
)
T5 = ToolName.GET_OPEN_ORDERS

# 정답과 같아야 하는 실행 열: 결과 열 + 실패 요약 4열(실패 문구는 뺌)
RESULT_COLUMNS = (
    "completed_status",
    "result_class",
    "status_reason",
    "dependency_scope",
    "decision_signature",
    "decision_signature_sha256",
    "model_name",
    "prompt_version",
    "final_explanation_json",
    "explanation_validation_status",
    "explanation_generated_at",
)
FAILURE_COLUMNS = ("failed_step", "failure_code", "attempt_count", "retry_exhausted")
ANSWER_COLUMNS = RESULT_COLUMNS + FAILURE_COLUMNS
# 저장 직후에만 뜻이 있는 열(정답과 견주지 않음)
STATE_COLUMNS = (
    "status",
    "started_at",
    "finished_at",
    "failure_message",
    "stale_primary_reason",
    "stale_reason_codes",
    "stale_detected_seq",
    "stale_detected_at",
)
SNAPSHOT_COLUMNS = ANSWER_COLUMNS + STATE_COLUMNS
ROW_PARTS = ("inventory", "risks", "options", "outcomes")

_RESET = text(
    "UPDATE analysis_runs SET status = 'DETECTED', completed_status = NULL, result_class = NULL,"
    " status_reason = NULL, failed_step = NULL, failure_code = NULL, attempt_count = NULL,"
    " retry_exhausted = NULL, failure_message = NULL, dependency_scope = NULL,"
    " decision_signature = NULL, decision_signature_sha256 = NULL, model_name = NULL,"
    " prompt_version = NULL, final_explanation_json = NULL, explanation_validation_status = NULL,"
    " explanation_generated_at = NULL, stale_primary_reason = NULL, stale_reason_codes = NULL,"
    " stale_detected_seq = NULL, stale_detected_at = NULL, finished_at = NULL"
    " WHERE analysis_run_id = :id"
)
_RUNS = text(
    """
    SELECT r.analysis_run_id, r.company_id, c.company_code, r.run_code, r.input_seq, r.delay_event_id,
           r.started_at, r.completed_status, r.failure_code
      FROM analysis_runs r JOIN companies c USING (company_id)
     ORDER BY c.company_code, r.input_seq, r.run_code
    """
)
_ROLE_EXISTS = text("SELECT 1 FROM pg_roles WHERE rolname = :role")


@dataclass(frozen=True)
class AnswerRun:
    """확정 DB 의 실행 하나. run_code 는 고객사끼리 겹쳐 (고객사 코드, run_code)로 고른다."""

    analysis_run_id: uuid.UUID
    company_id: uuid.UUID
    company_code: str
    run_code: str
    input_seq: int
    delay_event_id: uuid.UUID
    started_at: datetime
    completed_status: str
    failure_code: str | None

    @property
    def key(self) -> tuple[str, str]:
        return (self.company_code, self.run_code)

    @property
    def label(self) -> str:
        return f"{self.company_code}/{self.run_code}"


@dataclass
class RealPathRun:
    """실제 경로로 한 번 돌린 결과."""

    run: AnswerRun
    after: dict[str, Any]
    seconds: float
    states: list[dict[str, Any]] = field(default_factory=list)
    saved: list[Any] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)  # 저장하는 순간의 DB 역할

    @property
    def state(self) -> dict[str, Any]:
        return self.states[-1] if self.states else {}


def require_engine() -> None:
    settings = get_settings()
    if settings.glowship_engine_dir is None:
        pytest.skip("GLOWSHIP_ENGINE_DIR 이 없어 실제 경로 시험을 건너뜀")
    if settings.explanation_model != MOCK_EXPLANATION_MODEL:
        pytest.skip(
            "EXPLANATION_MODEL 이 mock 이 아니라 건너뜀(정답은 엔진 템플릿 설명)"
        )


def fix_explanation_canary(monkeypatch) -> None:
    """
    엔진 설명 생성기가 시도마다 만드는 난수 canary 를 고정한다. 난수가 우연히 금지 표현 검사(전화번호 모양)에
    걸려 설명이 0.7% 확률로 한 번 더 도는 일을 막는다(통합 시험 T13·T20 과 같은 처리).
    """
    generator = load_engine_module("explain.generator")
    monkeypatch.setattr(
        generator.secrets, "token_hex", lambda n: ("7e57ca9a" * n)[: 2 * n]
    )


async def answer_runs(db: AsyncSession) -> dict[tuple[str, str], AnswerRun]:
    """(고객사 코드, run_code) → 실행. (고객사, 입력 순번) 순이라 그대로 돌리면 엔진 저장소 캐시가 맞는다."""
    rows = (await db.execute(_RUNS)).all()
    return {(r.company_code, r.run_code): AnswerRun(*r) for r in rows}


async def pick_runs(db: AsyncSession, expected: dict[tuple[str, str], str]) -> list:
    """
    고른 실행이 이 DB 에 정답 모양 그대로 있을 때만 돌려준다. expected 값은 완료 상태 또는 실패 코드.
    하나도 없으면 건너뛰고(정답 overlay 가 아닌 DB), 일부만 없거나 모양이 다르면 실패시킨다(조용히 줄지 않게).
    """
    found = await answer_runs(db)
    present = [key for key in expected if key in found]
    if not present:
        pytest.skip("정답 overlay 실행이 없는 DB 라 건너뜀")
    wrong = {
        key: kind
        for key, kind in expected.items()
        if key not in found
        or kind not in (found[key].completed_status, found[key].failure_code)
    }
    assert not wrong, f"정답 실행이 없거나 모양이 다름: {wrong}"
    return [found[key] for key in found if key in expected]


async def answer_snapshot(db: AsyncSession, run: AnswerRun) -> dict[str, Any]:
    """되돌리기 전 행 = 정답."""
    return await _snapshot(db, run.analysis_run_id, SNAPSHOT_COLUMNS)


async def reset_to_detected(db: AsyncSession, run: AnswerRun) -> None:
    await db.execute(_RESET, {"id": run.analysis_run_id})  # 실패 요약 먼저(ck10)
    await _clear(db, run.analysis_run_id)
    await db.execute(_RESET, {"id": run.analysis_run_id})
    await db.commit()  # 롤백 세션에서는 SAVEPOINT 만 바뀐다(실행기 세션과 맞춤)


def inject_answer_fault(runtime: AnalysisRuntime, failure_code: str | None) -> None:
    """정답 RT-1·RT-2 를 만든 장애를 실제 묶음에 건다. 다른 실행에는 아무것도 하지 않는다."""
    if failure_code == EXPLANATION_GENERATION_FAILED:  # RT-2: 설명 결함 3개
        mock = load_engine_module("explain.mock")
        runtime.explain = make_explainer(
            adapter=mock.MockAdapter(
                load_explanation_assets()["labels"], dict(RT2_FAULTS)
            )
        )
    elif failure_code == TOOL_TIMEOUT_EXHAUSTED:  # RT-1: T5 가 시도별 시간 제한을 넘김

        async def slow(ctx, payload):
            await asyncio.sleep(1.0)

        tools = dict(runtime.registry)
        tools[T5] = replace(tools[T5], fn=slow, timeout_seconds=0.01)
        runtime.registry = ToolRegistry(tools)


def app_like_runner(
    db: AsyncSession,
    *,
    tune: Callable[[AnalysisRuntime, StartedRun], None] | None = None,
    tracer_factory: TracerFactory | None = None,
    states: list[dict[str, Any]] | None = None,
    saved: list[Any] | None = None,
    roles: list[str] | None = None,
    max_concurrent_runs: int = 4,
) -> AnalysisRunner:
    """
    앱 실행기(build_app_runner, 가상 시각)와 같은 조립. 세션만 롤백 세션 하나를 돌려준다.
    tune 은 만들어진 실제 묶음에 장애를 걸 때만 쓴다. states·saved 는 그래프 종료 상태와 저장 결과를,
    roles 는 저장하는 순간의 DB 역할(current_user)을 받아 둔다.
    """

    @asynccontextmanager
    async def same_session():
        # 앱은 세션마다 새 접속이라 RLS 문맥이 비어 있다. 세션 하나를 돌려쓰면 앞에서 넣은 문맥이 남으므로 비운다
        await db.execute(_EMPTY_RLS_CONTEXT)
        yield db

    def runtime_factory(session, run, tracer):
        started = run.started_at.replace(microsecond=0)
        runtime = build_engine_runtime(
            session, run, clock=lambda: started, tracer=tracer
        )
        if tune is not None:
            tune(runtime, run)
        return runtime

    async def save(db_, run, state, *, now=None):
        if states is not None:
            states.append(state)
        if roles is not None:
            roles.append(await db_.scalar(text("SELECT current_user")))
        outcome = await save_graph_outcome(db_, run, state, now=now)
        if saved is not None:
            saved.append(outcome)
        return outcome

    return AnalysisRunner(
        open_session=same_session,
        runtime_factory=runtime_factory,
        save_time=lambda run: run.started_at,
        save_outcome=save,
        tracer_factory=tracer_factory,
        max_concurrent_runs=max_concurrent_runs,
    )


@asynccontextmanager
async def worker_role(db: AsyncSession):
    """워커 역할로 바꿨다가 되돌린다. 로컬 개발 DB 는 superuser 라 SET LOCAL ROLE 로 역할을 건다."""
    if await db.scalar(_ROLE_EXISTS, {"role": WORKER_ROLE}) is None:
        pytest.skip(f"{WORKER_ROLE} 역할이 없는 DB 라 건너뜀")
    await db.execute(text(f"SET LOCAL ROLE {WORKER_ROLE}"))
    # 롤백 세션에서 커밋은 SAVEPOINT 를 놓는 것이라, 역할이 바깥 트랜잭션에 남는다.
    # 이렇게 해 두지 않으면 실행기의 rollback(SAVEPOINT 되돌리기)이 역할까지 되돌린다.
    await db.commit()
    try:
        yield
        role = await db.scalar(text("SELECT current_user"))
        assert role == WORKER_ROLE, f"워커 역할이 도중에 풀림(지금 {role})"
    finally:
        await db.execute(text("RESET ROLE"))
        db.expire_all()


async def run_to_end(
    db: AsyncSession,
    runner: AnalysisRunner,
    runs: list[tuple[uuid.UUID, uuid.UUID]],
    *,
    worker=True,
) -> float:
    """(고객사 ID, 실행 ID)들을 실행기로 시작해 끝날 때까지 기다린다. 걸린 시간(초)을 돌려준다."""
    started = time.perf_counter()

    async def go():
        for company_id, run_id in runs:
            assert runner.start(company_id, run_id)
        await asyncio.wait_for(runner.wait_idle(), 600)

    if worker:
        async with worker_role(db):
            await go()
    else:
        await go()
    db.expire_all()
    return time.perf_counter() - started


async def rerun(
    db: AsyncSession,
    run: AnswerRun,
    *,
    inject: bool = True,
    tracer_factory: TracerFactory | None = None,
    tune: Callable[[AnalysisRuntime, StartedRun], None] | None = None,
) -> RealPathRun:
    """실행 하나를 DETECTED 로 되돌려 실제 경로(워커 역할)로 끝까지 돌리고, 저장 뒤 행을 돌려준다."""

    def tuned(runtime, started_run):
        if inject:
            inject_answer_fault(runtime, run.failure_code)
        if tune is not None:
            tune(runtime, started_run)

    states: list[dict[str, Any]] = []
    saved: list[Any] = []
    roles: list[str] = []
    await reset_to_detected(db, run)
    runner = app_like_runner(
        db,
        tune=tuned,
        tracer_factory=tracer_factory,
        states=states,
        saved=saved,
        roles=roles,
    )
    seconds = await run_to_end(db, runner, [(run.company_id, run.analysis_run_id)])
    after = await _snapshot(db, run.analysis_run_id, SNAPSHOT_COLUMNS)
    return RealPathRun(run, after, seconds, states, saved, roles)


def _short(value: Any, limit: int = 160) -> str:
    shown = repr(value)
    return shown if len(shown) <= limit else f"{shown[:limit]}…({len(shown)}자)"


def differences(
    answer: dict[str, Any],
    after: dict[str, Any],
    *,
    columns: tuple[str, ...] = ANSWER_COLUMNS,
    parts: tuple[str, ...] = ROW_PARTS,
) -> list[str]:
    """정답 행과 저장 뒤 행이 다른 곳. 같으면 빈 목록."""
    found = [
        f"실행 열 {name}: 정답 {_short(answer['run'][name])} ≠ 저장 {_short(after['run'][name])}"
        for name in columns
        if answer["run"][name] != after["run"][name]
    ]
    for part in parts:
        expected, got = answer[part], after[part]
        if expected == got:
            continue
        if expected.keys() != got.keys():
            found.append(f"{part} 행 수·키: 정답 {len(expected)}건 ≠ 저장 {len(got)}건")
            continue
        changed = sorted(
            {
                name
                for key in expected
                for name in expected[key]
                if expected[key][name] != got[key].get(name)
            }
        )
        found.append(f"{part} 행 값이 다른 열: {changed}")
    return found


def state_problems(run: RealPathRun) -> list[str]:
    """저장 직후 상태: 워커 역할로 저장, 상태 = 완료 상태, 끝난 시각 = 시작 + 1초, STALE 열 비어 있음."""
    row = run.after["run"]
    found = []
    if run.roles != [WORKER_ROLE]:
        found.append(f"저장할 때 DB 역할 {run.roles} ≠ [{WORKER_ROLE}]")
    if row["status"] != row["completed_status"]:
        found.append(f"상태 {row['status']} ≠ 완료 상태 {row['completed_status']}")
    if row["finished_at"] != row["started_at"] + timedelta(seconds=1):
        found.append(f"끝난 시각 {row['finished_at']} ≠ 시작 + 1초")
    stale = {
        name: row[name]
        for name in STATE_COLUMNS
        if name.startswith("stale_") and row[name] is not None
    }
    if stale:
        found.append(f"STALE 열이 비어 있지 않음: {stale}")
    return found
