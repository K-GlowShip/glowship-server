import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import AbstractAsyncContextManager, aclosing, asynccontextmanager
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
from app.modules.delay_analysis.application.agent.retry import (
    RUN_TIME_LIMIT_EXCEEDED,
    RUN_TIME_LIMIT_MESSAGE,
)
from app.modules.delay_analysis.application.agent.runtime import (
    RUNTIME_KEY,
    AnalysisRuntime,
)
from app.modules.delay_analysis.application.agent.state import AnalysisStep
from app.modules.delay_analysis.application.agent.tracing import RunTracer
from app.modules.delay_analysis.application.result_save.result_save_service import (
    ResultSaveOutcome,
)
from app.modules.delay_analysis.application.run_start.outcome_save import (
    save_graph_outcome,
)
from app.modules.delay_analysis.application.run_start.transitions import (
    IN_PROGRESS_STEPS,
    RESTART_FAILURE_MESSAGE,
    RetireOutcome,
    StartedRun,
    UnfinishedRun,
    advance_status,
    begin_analysis,
    fail_unfinished,
    find_unfinished_runs,
    retire_unstarted_run_of_closed_episode,
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
        단 닫힌 지연의 회복 실행이 아닌 DETECTED 는 시작하지 않고 STALE 로 끝낸다(이슈 #51, 사용자 결정 11-11,
        transitions.retire_unstarted_run_of_closed_episode).
        서버 프로세스가 하나라는 전제다(둘 이상이면 다른 프로세스가 돌리는 실행을 FAILED 로 만든다, docs/analysis/auto-start.md 5절).
        실행 하나를 FAILED 로 바꾸다 예외가 나면 로그만 남기고 다음 실행으로 넘어간다(서버가 뜨지 않게 하지 않음).
        남은 실행 조회 자체가 실패하면 그대로 올려 서버 시작을 멈춘다(설정·권한 잘못이라 바로 알려야 함).
      - 종료 shutdown: 돌던 작업을 유예 시간만큼 기다리고 남은 것은 취소한다.
      - 추적(tracer_factory, docs/agent-flow/agent-tracing.md): 실행마다 시작 전이 뒤 추적기를 하나 만들어 실행 도구 묶음에
        넘기고(도구·설명 시도), 그래프 콜백으로도 넘겨(실행·노드·갈림길 사건) 실행 하나의 사건이 한 추적기로 이어지게 한다.
        끝나면 aclose() 로 닫는다(동기 close 는 이벤트 루프를 멈춰 다른 실행의 도구가 시간 초과로 잡힘, PR #42 리뷰).
        추적기를 만들거나 닫다 예외가 나도 분석은 계속한다(agentflow 8.1 "장애 영향"). 만들기에 실패하면 추적 없이 돈다.
        없으면(기본) 추적하지 않는다.
      - 메모리(이슈 #123): 그래프가 끝나면(예외 포함) 실행 묶음의 엔진 입력을 놓는다(AnalysisRuntime.release).
      - 시간 제한(이슈 #123, docs/analysis/auto-start.md 2절): 두 구간. 기본 None(제한 없음), 앱 조립이 설정값을 넘긴다.
          · 분석 = 시작 전이 ~ 그래프 끝(자리를 기다린 시간은 뺌). 넘으면 그래프를 취소하고 새 세션으로 지금 단계
            FAILED(RUN_TIME_LIMIT_EXCEEDED). 시작 전이에서 넘으면 실행은 DETECTED 그대로(다음 시작 정리가 다시 시작).
          · 저장 = 결과 저장(저장 직후 재판정의 판정 서명 재계산 포함). 넘으면 되돌리고 재계산 없이 다시 저장한다.
            재계산은 걸린 변경이 있을 때만 돌므로 다시 저장은 사실상 STALE(놓치지 않는 쪽). 그것도 넘으면 FAILED.
          · 넘었는지는 예외 종류가 아니라 제한의 expired() 로 본다(취소 뒤 세션 되돌리기가 다른 예외를 낼 수 있음).
            서버 종료의 취소(CancelledError)는 제한으로 바꾸지 않는다(다음 시작 정리가 재시작 문구로 FAILED).
          · 스레드(저장소 읽기·엔진 계산)는 취소돼도 끝까지 돌고 결과는 버린다(캐시에도 안 넣음).
"""

logger = logging.getLogger(__name__)

SessionOpener = Callable[[], AbstractAsyncContextManager[AsyncSession]]
# 실행 하나의 실행 도구 묶음을 만든다(그래프 세션, 시작한 실행, 추적기 또는 None)
RuntimeFactory = Callable[[AsyncSession, StartedRun, RunTracer | None], AnalysisRuntime]
# 실행 하나의 추적기를 만든다(없으면 None = 추적 안 함). 닫기는 실행기가 한다
TracerFactory = Callable[[StartedRun], RunTracer | None]
# 그래프 종료 상태를 저장한다(save_graph_outcome 과 같은 모양, 시험에서 바꿈)
OutcomeSaver = Callable[..., Awaitable[ResultSaveOutcome | None]]
# 저장 끝난 시각으로 넘길 값. 가상 시각이면 실행 시작 시각, 아니면 None(DB 시각)
SaveTime = Callable[[StartedRun | AnalysisRun], datetime | None]

# 시간 제한을 켰을 때 뒷정리(재계산 없이 다시 저장, 제한 FAILED 쓰기)에 주는 시간(초). 이것도 막히면 critical 로그만
# 남기고 자리를 푼다(그 실행은 다음 시작 정리가 끝냄).
CLEANUP_LIMIT_SECONDS = 30.0


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
    # 닫힌 지연의 시작 전 실행을 STALE 로 끝냄(시작하지 않음)
    retired: list[uuid.UUID] = field(default_factory=list)
    # 닫힌 지연인데 닫은 수집을 못 찾아 그대로 둔 DETECTED 실행(시작하지 않음, 다음 시작 정리가 다시 봄)
    left_detected: list[uuid.UUID] = field(default_factory=list)
    # FAILED·STALE 로 바꾸다 예외가 난 실행(그 상태로 남음, 다음 시작 정리가 다시 봄)
    errors: list[uuid.UUID] = field(default_factory=list)


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
        tracer_factory: TracerFactory | None = None,
        run_time_limit_seconds: float | None = None,
        save_time_limit_seconds: float | None = None,
    ):
        self.open_session = open_session
        self.runtime_factory = runtime_factory
        self.tracer_factory = tracer_factory
        self.enabled = enabled
        self.shutdown_grace_seconds = shutdown_grace_seconds
        self.save_time = save_time
        self.save_outcome = save_outcome
        self.graph = graph or build_analysis_graph()
        self._semaphore = asyncio.Semaphore(max_concurrent_runs)
        self.run_time_limit_seconds = run_time_limit_seconds
        self.save_time_limit_seconds = save_time_limit_seconds
        limited = (
            run_time_limit_seconds is not None or save_time_limit_seconds is not None
        )
        self._cleanup_limit = CLEANUP_LIMIT_SECONDS if limited else None
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
        run: StartedRun | None = None
        tracer: RunTracer | None = None
        limit = asyncio.timeout(self.run_time_limit_seconds)
        try:
            try:
                async with limit:
                    run = await self._begin(company_id, analysis_run_id)
                    if run is None:
                        return
                    tracer = self._open_tracer(run)
                    final = await self._run_graph(run, tracer)
            except Exception:
                if not limit.expired():
                    raise
                await self._fail_on_limit(
                    company_id, analysis_run_id, "분석", self.run_time_limit_seconds
                )
                return
            saved = await self._save(run, final or {})
        finally:
            if run is not None:
                await self._close_tracer(run, tracer)
        logger.info(
            "분석 실행 %s 끝: %s (저장 %s)",
            analysis_run_id,
            (final or {}).get("outcome"),
            None if saved is None else saved.status,
        )

    async def _run_graph(
        self, run: StartedRun, tracer: RunTracer | None
    ) -> dict[str, Any] | None:
        """그래프를 끝까지 돌리며 단계 전이를 하고 마지막 상태를 돌려준다."""
        step = AnalysisStep.VALIDATING
        final: dict[str, Any] | None = None
        runtime: AnalysisRuntime | None = None
        async with self.open_session() as graph_db:
            try:
                await set_rls_context(graph_db, user_id=None, company_id=run.company_id)
                runtime = self.runtime_factory(graph_db, run, tracer)
                config: dict[str, Any] = {
                    "configurable": {
                        RUNTIME_KEY: runtime,
                        "run_status_reader": AnalysisRunStatusReader(
                            lambda: _reuse(graph_db), run.company_id
                        ),
                    }
                }
                if tracer is not None:
                    config["callbacks"] = [tracer.callback_handler()]
                # aclosing: 단계 전이 도중 취소·예외로 빠져도 그래프를 먼저 닫고 나서 그래프 세션을 되돌린다
                # (그냥 두면 생성기가 나중에 정리돼, 그사이 닫힌 세션을 쓰는 노드 작업이 남을 수 있음, 이슈 #123)
                async with aclosing(
                    self.graph.astream(initial_state(run), config, stream_mode="values")
                ) as stream:
                    async for state in stream:
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
                # 끝난 실행의 엔진 입력을 놓는다(이슈 #123). 노드 안에서 걸린 타이머가 취소돼도 예정 시각까지 이 실행의
                # 문맥(그래프 설정 → 실행 묶음)을 붙잡아, 놓지 않으면 엔진 저장소가 실행이 끝난 뒤에도 남는다.
                if runtime is not None:
                    runtime.release()
                # 그래프 세션은 읽기만 한다. 실패한 트랜잭션이어도 되돌리고 닫는다(저장은 새 세션)
                await graph_db.rollback()
        return final

    async def _save(
        self, run: StartedRun, final: dict[str, Any]
    ) -> ResultSaveOutcome | None:
        """그래프 종료 상태를 저장한다. 저장 제한을 넘으면 재계산 없이 다시 저장하고, 그것도 넘으면 FAILED."""
        limit = asyncio.timeout(self.save_time_limit_seconds)
        try:
            async with limit:
                return await self._save_once(run, final)
        except Exception:
            if not limit.expired():
                raise
        logger.warning(
            "분석 실행 %s 의 저장이 제한 %s초를 넘어 판정 서명 재계산 없이 다시 저장합니다(걸린 변경이 있으면 STALE)",
            run.analysis_run_id,
            self.save_time_limit_seconds,
        )
        retry = asyncio.timeout(self._cleanup_limit)
        try:
            async with retry:
                return await self._save_once(run, final, recalculate=False)
        except Exception:
            if not retry.expired():
                raise
        await self._fail_on_limit(
            run.company_id, run.analysis_run_id, "저장", self.save_time_limit_seconds
        )
        return None

    async def _save_once(
        self, run: StartedRun, final: dict[str, Any], **options: Any
    ) -> ResultSaveOutcome | None:
        # options 는 다시 저장할 때만 넘긴다(recalculate=False). 저장 함수를 바꿔 끼운 시험·장치는 처음 저장만 쓴다
        async with self.open_session() as db:
            await set_rls_context(db, user_id=None, company_id=run.company_id)
            saved = await self.save_outcome(
                db, run, final, now=self.save_time(run), **options
            )
            await db.commit()
            return saved

    async def _fail_on_limit(
        self,
        company_id: uuid.UUID,
        analysis_run_id: uuid.UUID,
        part: str,
        seconds: float | None,
    ) -> None:
        """제한을 넘은 실행을 새 세션으로 지금 단계 FAILED(RUN_TIME_LIMIT_EXCEEDED). 막히면 critical 로그(다음 시작 정리가 끝냄)."""
        logger.warning(
            "분석 실행 %s 의 %s 구간이 제한 %s초를 넘어 중단합니다",
            analysis_run_id,
            part,
            seconds,
        )
        try:
            async with asyncio.timeout(self._cleanup_limit):
                async with self.open_session() as db:
                    outcome = await fail_unfinished(
                        db,
                        company_id=company_id,
                        analysis_run_id=analysis_run_id,
                        message=RUN_TIME_LIMIT_MESSAGE,
                        code=RUN_TIME_LIMIT_EXCEEDED,
                        now=await self._save_time_of(db, company_id, analysis_run_id),
                    )
                    await db.commit()
        except Exception:
            logger.critical(
                "분석 실행 %s 를 제한 초과 FAILED 로 저장하지 못했습니다. 다음 서버 시작 정리가 끝냅니다",
                analysis_run_id,
                exc_info=True,
            )
            return
        if outcome is None:
            logger.warning(
                "분석 실행 %s 는 진행 중이 아니라(시작 전이에서 넘었거나 이미 끝남) FAILED 로 바꾸지 않았습니다",
                analysis_run_id,
            )

    def _open_tracer(self, run: StartedRun) -> RunTracer | None:
        """추적기를 만든다. 만들다 예외가 나면 경고만 남기고 추적 없이 분석한다(agentflow 8.1 장애 영향)."""
        if self.tracer_factory is None:
            return None
        try:
            return self.tracer_factory(run)
        except Exception:
            logger.warning(
                "분석 실행 %s 의 추적기를 만들지 못해 추적 없이 분석합니다",
                run.analysis_run_id,
                exc_info=True,
            )
            return None

    async def _close_tracer(self, run: StartedRun, tracer: RunTracer | None) -> None:
        """추적 쓰기를 기다리고 닫는다. 예외는 경고만(분석 결과·저장에 영향 없음)."""
        if tracer is None:
            return
        try:
            await tracer.aclose()
        except Exception:
            logger.warning(
                "분석 실행 %s 의 추적기를 닫지 못했습니다(분석은 그대로)",
                run.analysis_run_id,
                exc_info=True,
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
        """끝나지 않은 실행 정리: DETECTED 는 다시 시작(닫힌 지연의 비회복 실행은 STALE), 진행 중은 그때 단계로 FAILED."""
        result = SweepResult()
        if not self.enabled:
            return result
        async with self.open_session() as db:
            unfinished = await find_unfinished_runs(db)
            await db.rollback()
        for item in unfinished:
            if item.status == AnalysisRunStatus.DETECTED:
                try:
                    retired = await self._retire_if_closed(item)
                except Exception:
                    # 실행 하나 때문에 서버가 뜨지 않게 하지 않는다. DETECTED 로 남아 다음 시작 정리가 다시 본다
                    logger.exception(
                        "서버 시작 정리: 분석 실행 %s 의 지연 종료 확인에 실패했습니다(시작하지 않음, 다음 시작 때 다시 정리)",
                        item.analysis_run_id,
                    )
                    result.errors.append(item.analysis_run_id)
                    continue
                if retired == RetireOutcome.RETIRED:
                    result.retired.append(item.analysis_run_id)
                elif retired == RetireOutcome.CLOSE_SYNC_UNKNOWN:
                    result.left_detected.append(item.analysis_run_id)
                elif self.start(item.company_id, item.analysis_run_id):
                    result.restarted.append(item.analysis_run_id)
                continue
            try:
                saved = await self._fail_on_restart(item)
            except Exception:
                # 실행 하나 때문에 서버가 뜨지 않게 하지 않는다. 그 실행은 진행 중으로 남아 다음 시작 정리가 다시 본다
                logger.exception(
                    "서버 시작 정리: 분석 실행 %s 를 FAILED 로 바꾸지 못했습니다(다음 시작 때 다시 정리)",
                    item.analysis_run_id,
                )
                result.errors.append(item.analysis_run_id)
                continue
            if saved is not None and saved.saved:
                result.failed.append(item.analysis_run_id)
        logger.info(
            "서버 시작 정리: 다시 시작 %d건, 분석 도중 멈춰 FAILED %d건, 닫힌 지연이라 STALE %d건,"
            " 닫은 수집을 못 찾아 그대로 %d건, 정리 실패 %d건",
            len(result.restarted),
            len(result.failed),
            len(result.retired),
            len(result.left_detected),
            len(result.errors),
        )
        return result

    async def _retire_if_closed(self, item: UnfinishedRun) -> RetireOutcome:
        async with self.open_session() as db:
            outcome = await retire_unstarted_run_of_closed_episode(
                db, company_id=item.company_id, analysis_run_id=item.analysis_run_id
            )
            await (db.commit() if outcome == RetireOutcome.RETIRED else db.rollback())
        return outcome

    async def _fail_on_restart(self, item: UnfinishedRun) -> ResultSaveOutcome | None:
        async with self.open_session() as db:
            saved = await fail_unfinished(
                db,
                company_id=item.company_id,
                analysis_run_id=item.analysis_run_id,
                message=RESTART_FAILURE_MESSAGE,
                now=await self._save_time_of(db, item.company_id, item.analysis_run_id),
            )
            await db.commit()
        return saved

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
