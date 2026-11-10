from types import SimpleNamespace

import pytest

from app import main as app_main
from app.main import app, lifespan
from app.modules.delay_analysis.infrastructure.engine import (
    engine_loader,
    engine_startup,
)
from app.modules.delay_analysis.infrastructure.engine.engine_loader import (
    ENGINE_PACKAGE,
    EngineIntegrityError,
    EngineNotConfiguredError,
)

"""
    서버 시작 때 엔진 확인 (engine_startup.verify_engine_on_startup, app.main.lifespan)
      - 경로가 없으면 건너뛰고 서버는 뜸 / 경로가 틀렸거나 원본이 목록과 다르면 시작 단계에서 멈춤
      - 엔진 없이 돎(임시 폴더). 실제 엔진 대조는 test_engine_loader 가 본다
      - 시작 단계는 분석 자동 시작(GLOW-128)의 켜기 확인·시작 정리·종료도 부른다. 시험에서는 기록만 하는 가짜로 바꾼다:
        진짜 실행기면 .env 에서 자동 시작을 켠 채 시험을 돌릴 때 개발 DB 의 진행 중 실행을 FAILED 로 커밋하고
        남은 실행을 시험 프로세스 안에서 분석하기 때문이다.
"""


class _RecordingRunner:
    def __init__(self, calls):
        self.calls = calls

    async def sweep_on_startup(self):
        self.calls.append("sweep")

    async def shutdown(self):
        self.calls.append("shutdown")


@pytest.fixture
def runner_calls(monkeypatch):
    """lifespan 이 부르는 자동 시작 함수를 기록만 하는 가짜로 바꾼다(실제 DB·설정을 건드리지 않음)."""
    calls: list[str] = []
    runner = _RecordingRunner(calls)
    monkeypatch.setattr(
        app_main, "check_auto_start_ready", lambda _settings: calls.append("check")
    )
    monkeypatch.setattr(app_main, "get_analysis_runner", lambda: runner)
    return calls


@pytest.fixture
def engine_dir_setting(monkeypatch):
    """두 모듈이 읽는 설정을 바꾸고, 해시 대조 캐시를 시험 앞뒤로 비운다."""
    engine_loader.load_verified_engine_dir.cache_clear()

    def use(path):
        settings = SimpleNamespace(glowship_engine_dir=path)
        monkeypatch.setattr(engine_startup, "get_settings", lambda: settings)
        monkeypatch.setattr(engine_loader, "get_settings", lambda: settings)

    yield use
    engine_loader.load_verified_engine_dir.cache_clear()


def test_startup_skips_when_engine_dir_not_set(engine_dir_setting):
    engine_dir_setting(None)

    assert engine_startup.verify_engine_on_startup() is False


def test_startup_fails_when_engine_folder_missing(engine_dir_setting, tmp_path):
    engine_dir_setting(tmp_path)  # glowship_engine 폴더 없음

    with pytest.raises(EngineNotConfiguredError):
        engine_startup.verify_engine_on_startup()


def test_startup_fails_when_engine_files_differ_from_manifest(
    engine_dir_setting, tmp_path
):
    (tmp_path / ENGINE_PACKAGE).mkdir()  # 폴더는 있지만 목록의 파일이 없음
    engine_dir_setting(tmp_path)

    with pytest.raises(EngineIntegrityError):
        engine_startup.verify_engine_on_startup()


async def test_app_lifespan_runs_engine_check(
    engine_dir_setting, tmp_path, runner_calls
):
    engine_dir_setting(None)
    async with lifespan(app):
        pass  # 경로가 없으면 서버 시작은 그대로 된다

    engine_dir_setting(tmp_path)
    with pytest.raises(EngineNotConfiguredError):
        async with lifespan(app):
            pass


async def test_app_lifespan_checks_sweeps_and_shuts_down_runner(
    engine_dir_setting, runner_calls
):
    """켜기 확인 → 시작 정리 → (서버 동작) → 종료 순서로 부른다."""
    engine_dir_setting(None)
    async with lifespan(app):
        runner_calls.append("serving")

    assert runner_calls == ["check", "sweep", "serving", "shutdown"]
