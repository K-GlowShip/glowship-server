from types import SimpleNamespace

import pytest

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
"""


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


async def test_app_lifespan_runs_engine_check(engine_dir_setting, tmp_path):
    engine_dir_setting(None)
    async with lifespan(app):
        pass  # 경로가 없으면 서버 시작은 그대로 된다

    engine_dir_setting(tmp_path)
    with pytest.raises(EngineNotConfiguredError):
        async with lifespan(app):
            pass
