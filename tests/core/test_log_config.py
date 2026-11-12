import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from app import main as app_main
from app.core.config import Settings

"""
    서버 로그 설정 (app/core/log_config.py, LOG_LEVEL)
      - 루트 로거는 프로세스 전체에 하나라, 출력을 붙이는 시험은 새 파이썬 프로세스에서 돌린다(pytest 는 루트에 이미 출력을 붙여 둠).
      - 앱 INFO 는 찍히고, 라이브러리 INFO(SQLAlchemy echo 처럼 자기 출력을 따로 붙인 것 포함)는 이 출력으로 다시 찍히지 않는다.
"""

PROJECT_ROOT = Path(__file__).resolve().parents[2]

REQUIRED = {
    "postgres_host": "db",
    "postgres_port": 5432,
    "postgres_user": "app_login",
    "postgres_password": "app_pw",
    "postgres_db": "glowship",
    "jwt_secret_key": "x" * 32,
}


def _run(code: str) -> subprocess.CompletedProcess:
    done = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=PROJECT_ROOT,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=60,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    return done


_PRELUDE = """
import logging, sys
from app.core.log_config import configure_app_logging
"""


def test_app_info_is_printed_and_library_info_is_not():
    done = _run(
        _PRELUDE
        + """
configure_app_logging("INFO")
logging.getLogger("app.modules.x").info("APP-INFO")
logging.getLogger("app.modules.x").debug("APP-DEBUG")
logging.getLogger("sqlalchemy.engine.Engine").info("LIB-INFO")
logging.getLogger("some.library").warning("LIB-WARNING")
"""
    )
    assert "INFO app.modules.x: APP-INFO" in done.stderr
    assert "APP-DEBUG" not in done.stderr
    assert "LIB-INFO" not in done.stderr
    assert "LIB-WARNING" in done.stderr  # 라이브러리 경고는 지금처럼 나옴


def test_library_with_its_own_handler_is_not_printed_twice():
    # SQLAlchemy echo=True 와 같은 모양: 라이브러리가 자기 출력을 붙이고 INFO 로 낸다
    done = _run(
        _PRELUDE
        + """
lib = logging.getLogger("sqlalchemy.engine.Engine")
lib.setLevel(logging.INFO)
own = logging.StreamHandler(sys.stdout)
own.setFormatter(logging.Formatter("OWN %(message)s"))
lib.addHandler(own)
configure_app_logging("INFO")
lib.info("SELECT 1")
"""
    )
    assert done.stdout.count("SELECT 1") == 1  # 라이브러리 자기 출력 한 번
    assert "SELECT 1" not in done.stderr  # 이 설정의 출력으로 다시 찍히지 않음


def test_calling_twice_adds_one_handler():
    done = _run(
        _PRELUDE
        + """
configure_app_logging("INFO")
configure_app_logging("INFO")
logging.getLogger("app.x").info("ONCE")
print("HANDLERS", len(logging.getLogger().handlers))
"""
    )
    assert done.stderr.count("ONCE") == 1
    assert "HANDLERS 1" in done.stdout


def test_existing_root_handler_is_kept_and_nothing_is_added():
    done = _run(
        _PRELUDE
        + """
mine = logging.StreamHandler(sys.stdout)
logging.getLogger().addHandler(mine)
configure_app_logging("WARNING")
print("HANDLERS", len(logging.getLogger().handlers), logging.getLogger().handlers[0] is mine)
print("APP LEVEL", logging.getLevelName(logging.getLogger("app").level))
"""
    )
    assert "HANDLERS 1 True" in done.stdout
    assert "APP LEVEL WARNING" in done.stdout


def test_warning_level_hides_app_info():
    done = _run(
        _PRELUDE
        + """
configure_app_logging("WARNING")
logging.getLogger("app.x").info("HIDDEN")
logging.getLogger("app.x").warning("SHOWN")
"""
    )
    assert "HIDDEN" not in done.stderr
    assert "SHOWN" in done.stderr


def test_log_level_default_and_lowercase():
    assert Settings(_env_file=None, **REQUIRED).log_level == "INFO"
    assert Settings(_env_file=None, **REQUIRED, log_level="debug").log_level == "DEBUG"


@pytest.mark.parametrize("value", ["VERBOSE", "", "10"])
def test_log_level_rejects_unknown(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **REQUIRED, log_level=value)


class _Runner:
    def __init__(self, calls):
        self.calls = calls

    async def sweep_on_startup(self):
        self.calls.append("sweep")

    async def shutdown(self):
        self.calls.append("shutdown")


async def test_lifespan_configures_logging_before_startup_checks(monkeypatch):
    # 시작 단계의 엔진·자동 시작 확인 로그(INFO)가 찍히려면 로그 설정이 그보다 먼저여야 한다
    calls: list[str] = []
    monkeypatch.setattr(
        app_main, "configure_app_logging", lambda level: calls.append(f"log:{level}")
    )
    monkeypatch.setattr(
        app_main, "verify_engine_on_startup", lambda: calls.append("engine")
    )
    monkeypatch.setattr(
        app_main, "check_explanation_model_ready", lambda _s: calls.append("model")
    )
    monkeypatch.setattr(
        app_main, "check_auto_start_ready", lambda _s: calls.append("auto")
    )
    monkeypatch.setattr(app_main, "get_analysis_runner", lambda: _Runner(calls))
    monkeypatch.setattr(
        app_main,
        "get_settings",
        lambda: Settings(_env_file=None, **REQUIRED, log_level="warning"),
    )
    async with app_main.lifespan(app_main.app):
        pass
    assert calls[0] == "log:WARNING"
    assert calls[1:] == ["engine", "model", "auto", "sweep", "shutdown"]
