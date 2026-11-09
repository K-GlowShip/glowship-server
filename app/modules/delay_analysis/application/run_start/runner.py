import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rls_context import set_rls_context
from app.modules.delay_analysis.application.agent.graph import (
    UNEXPECTED_ERROR_MESSAGE,
    build_analysis_graph,
)
from app.modules.delay_analysis.application.agent.runtime import (
    RUNTIME_KEY,
    AnalysisRuntime,
)
from app.modules.delay_analysis.application.agent.state import AnalysisStep
from app.modules.delay_analysis.application.result_save.result_save_service import (
    ResultSaveOutcome,
)
from app.modules.delay_analysis.application.run_start.outcome_save import (
    save_graph_outcome,
)
from app.modules.delay_analysis.application.run_start.transitions import (
    IN_PROGRESS_STEPS,
    RESTART_FAILURE_MESSAGE,
    StartedRun,
    advance_status,
    begin_analysis,
    fail_unfinished,
    find_unfinished_runs,
)
from app.modules.delay_analysis.infrastructure.analysis_enums import AnalysisRunStatus
from app.modules.delay_analysis.infrastructure.analysis_run import AnalysisRun
from app.modules.delay_analysis.infrastructure.repository.run_status_repository import (
    AnalysisRunStatusReader,
)

"""
    분석 실행기 (GLOW-128, 이슈 #51, docs/analysis/auto-start.md)
      - 서버 프로세스 안 asyncio 작업으로 돈다(사용자 결정 11-08, API 25 수동 수집의 백그라운드 적용과 같은 자리).
      - start(company_id, run_id): 실행을 만든 트랜잭션이 커밋된 뒤 부른다. 같은 실행이 이미 돌면 무시한다.
        동시 실행 수(세마포어)가 차면 DETECTED 그대로 기다린다.
      - 실행 하나 = 세 종류의 세션
          · 시작 전이·단계 전이·저장: 짧은 세션마다 하나(L2 만 잡고 바로 커밋)
          · 그래프(분석 준비의 엔진 입력, 노드 경계 상태 조회): 실행 동안 열어 두는 읽기 세션 하나
        저장을 그래프 세션과 나누는 이유: 분석 준비에서 DB 오류가 나면 그 세션은 실패한 트랜잭션이라 같은 세션으로
        저장할 수 없다(#68 리뷰). 저장은 새 세션이 하고, 그래프 세션은 끝날 때 되돌리고 닫는다.
      - 단계 전이: 그래프가 상태의 step 을 바꿀 때마다 같은 단계로 analysis_runs.status 를 바꾼다(진행 중·앞으로만).
      - 마지막 잡기(#68 결정 6의 나머지 반): 그래프 밖으로 나온 예외(연결 실수·LangGraph 오류·저장 예외)는 새 세션으로
        지금 단계 FAILED(UNEXPECTED_TOOL_ERROR)를 한 번 더 저장한다. 그것도 실패하면 critical 로그만 남기고,
        그 실행은 다음 서버 시작 정리가 FAILED 로 끝낸다. 취소(CancelledError)는 잡지 않는다(종료 때 다음 시작 정리로).
      - 서버 시작 정리 sweep_on_startup: DETECTED 는 다시 시작, 진행 중은 그때 단계로 FAILED(사용자 결정 11-08).
        서버 프로세스가 하나라는 전제다(둘 이상이면 다른 프로세스가 돌리는 실행을 FAILED 로 만든다, docs/analysis/auto-start.md 5절).
      - 종료 shutdown: 돌던 작업을 유예 시간만큼 기다리고 남은 것은 취소한다.
"""

logger = logging.getLogger(__name__)

SessionOpener = Callable[[], AbstractAsyncContextManager[AsyncSession]]
# 실행 하나의 실행 도구 묶음을 만든다(그래프 세션, 시작한 실행)
RuntimeFactory = Callable[[AsyncSession, StartedRun], AnalysisRuntime]
# 그래프 종료 상태를 저장한다(save_graph_outcome 과 같은 모양, 시험에서 바꿈)
OutcomeSaver = Callable[..., Awaitable[ResultSaveOutcome | None]]
# 저장 끝난 시각으로 넘길 값. 가상 시각이면 실행 시작 시각, 아니면 None(DB 시각)
SaveTime = Callable[[StartedRun | AnalysisRun], datetime | None]


def _no_save_time(_: StartedRun | AnalysisRun) -> datetime | None:
    return None


@asynccontextmanager
async def _reuse(db: AsyncSession) -> AsyncIterator[AsyncSession]:
    """상태 조회기(AnalysisRunStatusReader)가 그래프 세션을 닫지 않고 쓰게 한다."""
    yield db


@dataclass(frozen=True)
class SweepResult:
    restarted: list[uuid.UUID] = field(default_factory=list)
    failed: list[uuid.UUID] = field(default_factory=list)


def initial_state(run: StartedRun) -> dict[str, Any]:
    """서비스가 그래프에 넘기는 입력(agentflow 5.5 입력 표)."""
    return {
        "analysis_run_id": run.analysis_run_id,
        "company_id": run.company_id,
        "delay_event_id": run.delay_event_id,
        "operating_rule_set_id": run.operating_rule_set_id,
        "step": AnalysisStep.VALIDATING,
    }


class AnalysisRunner:
    def __init__(
        self,
        *,
        open_session: SessionOpener,
        runtime_factory: RuntimeFactory,
        enabled: bool = True,
        max_concurrent_runs: int = 4,
        shutdown_grace_seconds: float = 10.0,
        save_time: SaveTime = _no_save_time,
        save_outcome: OutcomeSaver = save_graph_outcome,
        graph: CompiledStateGraph | None = None,
    ):
        self.open_session = open_session
        self.runtime_factory = runtime_factory
        self.enabled = enabled
        self.shutdown_grace_seconds = shutdown_grace_seconds
        self.save_time = save_time
        self.save_outcome = save_outcome
        self.graph = graph or build_analysis_graph()
        self._semaphore = asyncio.Semaphore(max_concurrent_runs)
        self._tasks: dict[uuid.UUID, asyncio.Task] = {}

    # --- 시작 ------------------------------------------------------------------------

    def start(self, company_id: uuid.UUID, analysis_run_id: uuid.UUID) -> bool:
        """분석을 백그라운드로 시작한다. 꺼져 있거나 같은 실행이 이미 돌면 False."""
        if not self.enabled or analysis_run_id in self._tasks:
            return False
        task = asyncio.create_task(
            self._run(company_id, analysis_run_id),
            name=f"analysis-run-{analysis_run_id}",
        )
        self._tasks[analysis_run_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(analysis_run_id, None))
        return True

    def start_many(
        self, company_id: uuid.UUID, analysis_run_ids: Iterable[uuid.UUID]
    ) -> list[uuid.UUID]:
        """여러 실행을 시작하고 실제로 시작한 실행 ID 를 돌려준다(수집 한 번에 AUTO 실행이 여럿일 때)."""
        return [rid for rid in analysis_run_ids if self.start(company_id, rid)]

    @property
    def running(self) -> list[uuid.UUID]:
        return list(self._tasks)

    async def wait_idle(self) -> None:
        """지금 도는 작업이 모두 끝날 때까지 기다린다(시험·종료용)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)

    # --- 실행 하나 ---------------------------------------------------------------------

    async def _run(self, company_id: uuid.UUID, analysis_run_id: uuid.UUID) -> None:
        async with self._semaphore:
            try:
                await self._analyze(company_id, analysis_run_id)
            except Exception:
                logger.exception(
                    "분석 실행 %s 가 예외로 끝나 FAILED 로 남깁니다", analysis_run_id
                )
                await self._fail_last(company_id, analysis_run_id)

    async def _begin(
        self, company_id: uuid.UUID, analysis_run_id: uuid.UUID
    ) -> StartedRun | None:
        async with self.open_session() as db:
            run = await begin_analysis(
                db, company_id=company_id, analysis_run_id=analysis_run_id
            )
            if run is None:
                await db.rollback()
                return None
            await db.commit()
            return run

    async def _advance(self, run: StartedRun, to: AnalysisStep) -> None:
        async with self.open_session() as db:
            changed = await advance_status(
                db,
                company_id=run.company_id,
                analysis_run_id=run.analysis_run_id,
                to=to,
            )
            await (db.commit() if changed else db.rollback())

    async def _analyze(self, company_id: uuid.UUID, analysis_run_id: uuid.UUID) -> None:
        run = await self._begin(company_id, analysis_run_id)
        if run is None:
            return
        step = AnalysisStep.VALIDATING
        final: dict[str, Any] | None = None
        async with self.open_session() as graph_db:
            try:
                await set_rls_context(graph_db, user_id=None, company_id=company_id)
                config = {
                    "configurable": {
                        RUNTIME_KEY: self.runtime_factory(graph_db, run),
                        "run_status_reader": AnalysisRunStatusReader(
                            lambda: _reuse(graph_db), company_id
                        ),
                    }
                }
                async for state in self.graph.astream(
                    initial_state(run), config, stream_mode="values"
                ):
                    final = state
                    next_step = state.get("step")
                    if (
                        next_step in IN_PROGRESS_STEPS
                        and next_step != step
                        and "outcome" not in state
                    ):
                        step = next_step
                        await self._advance(run, step)
            finally:
                # 그래프 세션은 읽기만 한다. 실패한 트랜잭션이어도 되돌리고 닫는다(저장은 새 세션)
                await graph_db.rollback()
        async with self.open_session() as db:
            await set_rls_context(db, user_id=None, company_id=company_id)
            saved = await self.save_outcome(
                db, run, final or {}, now=self.save_time(run)
            )
            await db.commit()
        logger.info(
            "분석 실행 %s 끝: %s (저장 %s)",
            analysis_run_id,
            (final or {}).get("outcome"),
            None if saved is None else saved.status,
        )

    async def _fail_last(
        self, company_id: uuid.UUID, analysis_run_id: uuid.UUID
    ) -> None:
        """마지막 잡기: 새 세션으로 지금 단계 FAILED. 저장도 실패하면 critical 로그(다음 시작 정리가 끝냄)."""
        try:
            async with self.open_session() as db:
                outcome = await fail_unfinished(
                    db,
                    company_id=company_id,
                    analysis_run_id=analysis_run_id,
                    message=UNEXPECTED_ERROR_MESSAGE,
                    now=await self._save_time_of(db, company_id, analysis_run_id),
                )
                await db.commit()
            if outcome is None:
                logger.warning(
                    "분석 실행 %s 는 진행 중이 아니라 FAILED 로 바꾸지 않았습니다",
                    analysis_run_id,
                )
        except Exception:
            logger.critical(
                "분석 실행 %s 를 FAILED 로 저장하지 못했습니다. 다음 서버 시작 정리가 끝냅니다",
                analysis_run_id,
                exc_info=True,
            )

    async def _save_time_of(
        self, db: AsyncSession, company_id: uuid.UUID, analysis_run_id: uuid.UUID
    ) -> datetime | None:
        await set_rls_context(db, user_id=None, company_id=company_id)
        run = await db.get(AnalysisRun, analysis_run_id)
        return None if run is None else self.save_time(run)

    # --- 서버 시작·종료 ---------------------------------------------------------------

    async def sweep_on_startup(self) -> SweepResult:
        """끝나지 않은 실행 정리: DETECTED 는 다시 시작, 진행 중은 그때 단계로 FAILED."""
        result = SweepResult()
        if not self.enabled:
            return result
        async with self.open_session() as db:
            unfinished = await find_unfinished_runs(db)
            await db.rollback()
        for item in unfinished:
            if item.status == AnalysisRunStatus.DETECTED:
                if self.start(item.company_id, item.analysis_run_id):
                    result.restarted.append(item.analysis_run_id)
                continue
            async with self.open_session() as db:
                saved = await fail_unfinished(
                    db,
                    company_id=item.company_id,
                    analysis_run_id=item.analysis_run_id,
                    message=RESTART_FAILURE_MESSAGE,
                    now=await self._save_time_of(
                        db, item.company_id, item.analysis_run_id
                    ),
                )
                await db.commit()
            if saved is not None and saved.saved:
                result.failed.append(item.analysis_run_id)
        logger.info(
            "서버 시작 정리: 다시 시작 %d건, 분석 도중 멈춰 FAILED %d건",
            len(result.restarted),
            len(result.failed),
        )
        return result

    async def shutdown(self) -> None:
        """돌던 작업을 유예 시간만큼 기다리고 남은 것은 취소한다(취소된 실행은 다음 시작 정리가 FAILED 로)."""
        tasks = list(self._tasks.values())
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=self.shutdown_grace_seconds)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if pending:
            logger.warning(
                "서버 종료: 분석 실행 %d건을 끝내지 못하고 취소했습니다", len(pending)
            )
