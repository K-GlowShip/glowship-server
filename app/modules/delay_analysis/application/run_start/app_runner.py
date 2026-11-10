from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.database import open_worker_session
from app.modules.delay_analysis.application.agent.runtime import (
    AnalysisRuntime,
    utc_now_seconds,
)
from app.modules.delay_analysis.application.run_start.engine_runtime import (
    build_engine_runtime,
    missing_engine_tools,
)
from app.modules.delay_analysis.application.run_start.runner import AnalysisRunner
from app.modules.delay_analysis.application.run_start.transitions import StartedRun
from app.modules.delay_analysis.infrastructure.analysis_run import AnalysisRun

"""
    서버 하나의 분석 실행기 (GLOW-128, docs/analysis/auto-start.md 2절)
      - 서버 프로세스에 실행기 하나. 처음 부를 때 설정으로 만든다(get_analysis_runner).
      - 세션은 워커 역할(open_worker_session): 자동 실행·재분석의 분석에는 요청 사용자가 없다(#48 리뷰).
      - 시각(이슈 #50 결정): 가상 시각(VIRTUAL_CLOCK_START)이면 저장 끝난 시각·설명 생성 시각에 실행 시작 시각을 넘긴다
        (저장 함수가 끝난 시각 = 시작 + 1초를 보장, 설명 생성 ≤ 끝남). 아니면 넘기지 않는다(DB 시각·서버 시각).
      - 켜기 확인(check_auto_start_ready): 켰는데 실제 등록표에 빠진 도구가 있거나 엔진 경로(GLOWSHIP_ENGINE_DIR)가
        비어 있으면 서버 시작을 멈춘다. 켜 놓고 실행마다 FAILED 가 쌓이는 것보다 바로 알리는 쪽이다(T10 을 등록하기 전에는
        켜면 멈춤). 엔진 경로가 틀린 경우는 앞의 verify_engine_on_startup 이, 앱 역할만 넣고 워커 계정을 빠뜨린 경우는
        설정 검사(config._worker_credentials_come_together)가 이미 멈춘다.
"""

_runner: AnalysisRunner | None = None


class AutoStartNotReadyError(RuntimeError):
    """자동 시작을 켰지만 실제 실행 묶음에 필요한 도구가 다 등록되지 않았다."""


def _virtual(settings: Settings) -> bool:
    return settings.virtual_clock_start is not None


def build_app_runner(settings: Settings) -> AnalysisRunner:
    virtual = _virtual(settings)

    def runtime_factory(db: AsyncSession, run: StartedRun) -> AnalysisRuntime:
        started = run.started_at.replace(microsecond=0)
        return build_engine_runtime(
            db, run, clock=(lambda: started) if virtual else utc_now_seconds
        )

    def save_time(run: StartedRun | AnalysisRun) -> datetime | None:
        return run.started_at if virtual else None

    return AnalysisRunner(
        open_session=open_worker_session,
        runtime_factory=runtime_factory,
        enabled=settings.analysis_auto_start_enabled,
        max_concurrent_runs=settings.analysis_max_concurrent_runs,
        shutdown_grace_seconds=settings.analysis_shutdown_grace_seconds,
        save_time=save_time,
    )


def check_auto_start_ready(settings: Settings) -> None:
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
