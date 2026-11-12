import logging
import tempfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.database import open_worker_session
from app.modules.delay_analysis.application.agent.runtime import (
    AnalysisRuntime,
    utc_now_seconds,
)
from app.modules.delay_analysis.application.agent.tracing import (
    RunTracer,
    auto_tracing_env_on,
    build_run_tracer,
)
from app.modules.delay_analysis.application.run_start.engine_runtime import (
    build_engine_runtime,
    missing_engine_tools,
)
from app.modules.delay_analysis.application.run_start.runner import (
    AnalysisRunner,
    SessionOpener,
    TracerFactory,
)
from app.modules.delay_analysis.application.run_start.transitions import StartedRun
from app.modules.delay_analysis.infrastructure.agent_trace_outbox import (
    OUTBOX_DIR_NAME,
    TraceOutboxSink,
)
from app.modules.delay_analysis.infrastructure.analysis_run import AnalysisRun

"""
    서버 하나의 분석 실행기 (GLOW-128, docs/analysis/auto-start.md 2절)
      - 서버 프로세스에 실행기 하나. 처음 부를 때 설정으로 만든다(get_analysis_runner).
      - 세션은 워커 역할(open_worker_session): 자동 실행·재분석의 분석에는 요청 사용자가 없다(#48 리뷰).
      - 시각(이슈 #50 결정): 가상 시각(VIRTUAL_CLOCK_START)이면 저장 끝난 시각·설명 생성 시각에 실행 시작 시각을 넘긴다
        (저장 함수가 끝난 시각 = 시작 + 1초를 보장, 설명 생성 ≤ 끝남). 아니면 넘기지 않는다(DB 시각·서버 시각).
      - 켜기 확인(check_auto_start_ready): 켰는데 실제 등록표에 빠진 도구가 있거나, 엔진 경로(GLOWSHIP_ENGINE_DIR)가
        비어 있거나, LangChain·LangSmith 자동 추적 환경 변수가 켜져 있으면 서버 시작을 멈춘다. 켜 놓고 실행마다 FAILED 가
        쌓이는 것보다 바로 알리는 쪽이다. 자동 추적은 실행기 추적기와 상관없이 그래프 상태를 마스킹 없이 밖으로 보낼 수
        있어(agentflow 8.1) 실행마다가 아니라 여기서 한 번 본다. 엔진 경로가 틀린 경우는 앞의 verify_engine_on_startup 이,
        앱 역할만 넣고 워커 계정을 빠뜨린 경우는 설정 검사(config._worker_credentials_come_together)가 이미 멈춘다.
      - 추적(이슈 #51, docs/agent-flow/agent-tracing.md 3절): AGENT_TRACE_DIR 가 있으면 실행마다 추적기를 만들어 내부 로그
        (TraceOutboxSink)에 남긴다. 싱크는 실행기 하나에 하나를 모든 실행이 같이 쓴다(줄이 섞이지 않게 하는 잠금이 싱크마다라서).
        비어 있으면(기본) 추적하지 않는다(사용자 결정 11-11: 배포 컨테이너에 쓸 수 있는 폴더가 정해지기 전).
        파일 날짜는 서버 시각(UTC)이다. 가상 시각으로 쓰면 며칠치 실행이 한 파일에 몰린다.
      - open_session: 시험이 앱의 조립(가상 시각·저장 시각·추적기)을 그대로 쓰며 세션만 바꿀 때 넘긴다(기본 워커 세션).
"""

logger = logging.getLogger(__name__)

_runner: AnalysisRunner | None = None


class AutoStartNotReadyError(RuntimeError):
    """자동 시작을 켰지만 실제 실행 묶음에 필요한 도구가 다 등록되지 않았다."""


def _virtual(settings: Settings) -> bool:
    return settings.virtual_clock_start is not None


def build_tracer_factory(trace_dir: Path | None) -> TracerFactory | None:
    """실행마다 내부 로그 추적기를 만드는 함수. 폴더가 없으면 None(추적 안 함)."""
    if trace_dir is None:
        return None
    sink = TraceOutboxSink(trace_dir)

    def tracer_factory(run: StartedRun) -> RunTracer:
        # 자동 추적 환경 변수가 켜져 있으면 TraceConfigError — 실행기가 경고만 남기고 추적 없이 분석한다
        return build_run_tracer(run.analysis_run_id, run.company_id, [sink])

    return tracer_factory


def build_app_runner(
    settings: Settings, *, open_session: SessionOpener = open_worker_session
) -> AnalysisRunner:
    virtual = _virtual(settings)

    def runtime_factory(
        db: AsyncSession, run: StartedRun, tracer: RunTracer | None
    ) -> AnalysisRuntime:
        started = run.started_at.replace(microsecond=0)
        return build_engine_runtime(
            db,
            run,
            clock=(lambda: started) if virtual else utc_now_seconds,
            tracer=tracer,
        )

    def save_time(run: StartedRun | AnalysisRun) -> datetime | None:
        return run.started_at if virtual else None

    return AnalysisRunner(
        open_session=open_session,
        runtime_factory=runtime_factory,
        enabled=settings.analysis_auto_start_enabled,
        max_concurrent_runs=settings.analysis_max_concurrent_runs,
        shutdown_grace_seconds=settings.analysis_shutdown_grace_seconds,
        save_time=save_time,
        tracer_factory=build_tracer_factory(settings.agent_trace_dir),
        run_time_limit_seconds=settings.analysis_run_time_limit_seconds,
        save_time_limit_seconds=settings.analysis_save_time_limit_seconds,
    )


def check_auto_start_ready(
    settings: Settings, environ: Mapping[str, str] | None = None
) -> None:
    """environ: 시험용(없으면 os.environ)."""
    if not settings.analysis_auto_start_enabled:
        return
    missing = missing_engine_tools()
    if missing:
        raise AutoStartNotReadyError(
            "ANALYSIS_AUTO_START_ENABLED 를 켰지만 실제 실행 묶음에 등록되지 않은 도구가 있습니다:"
            f" {[name.value for name in missing]}. 도구를 등록하기 전에는 끄고 시작하세요(docs/analysis/auto-start.md)"
        )
    if settings.glowship_engine_dir is None:
        raise AutoStartNotReadyError(
            "ANALYSIS_AUTO_START_ENABLED 를 켰지만 GLOWSHIP_ENGINE_DIR 가 비어 있습니다. 엔진 없이는 실행마다"
            " 분석 준비에서 FAILED 가 됩니다(docs/analysis/engine-local-path.md)"
        )
    tracing_env = auto_tracing_env_on(environ)
    if tracing_env:
        raise AutoStartNotReadyError(
            f"ANALYSIS_AUTO_START_ENABLED 를 켰지만 LangChain 자동 추적 환경 변수가 켜져 있습니다: {tracing_env}."
            " 그래프 상태가 마스킹 없이 밖으로 나갈 수 있습니다. 끄고 시작하세요(docs/agent-flow/agent-tracing.md)"
        )
    _check_trace_dir(settings.agent_trace_dir)


def _check_trace_dir(trace_dir: Path | None) -> None:
    """켰는데 추적 폴더가 비어 있으면 경고만, 넣었는데 쓸 수 없으면 멈춤(실행마다 쓰기 경고가 쌓이는 것보다 바로 알림)."""
    if trace_dir is None:
        logger.warning(
            "자동 시작을 켰지만 AGENT_TRACE_DIR 가 비어 있어 실행 추적 내부 로그를 남기지 않습니다"
            "(docs/agent-flow/agent-tracing.md 3절)"
        )
        return
    outbox = Path(trace_dir) / OUTBOX_DIR_NAME
    try:
        outbox.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=outbox):
            pass
    except OSError as error:
        raise AutoStartNotReadyError(
            f"ANALYSIS_AUTO_START_ENABLED 를 켰지만 AGENT_TRACE_DIR({trace_dir}) 에 쓸 수 없습니다: {type(error).__name__}."
            " 서버 실행 사용자가 쓸 수 있는 폴더를 넣거나 비우세요(docs/agent-flow/agent-tracing.md 3절)"
        ) from error


def get_analysis_runner() -> AnalysisRunner:
    """서버의 분석 실행기. 꺼져 있으면 start 가 아무것도 하지 않는다(실행은 DETECTED 로 남음)."""
    global _runner
    if _runner is None:
        _runner = build_app_runner(get_settings())
    return _runner


def set_analysis_runner(runner: AnalysisRunner | None) -> None:
    """시험에서 실행기를 바꾼다. None 이면 다음 get_analysis_runner 가 설정으로 다시 만든다."""
    global _runner
    _runner = runner
