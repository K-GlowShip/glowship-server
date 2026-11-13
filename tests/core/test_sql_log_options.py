import logging

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, text

from app.core import database
from app.core.config import Settings, get_settings

"""
    SQL 로그 설정 (이슈 #141)
      - 기본은 끔(DB_ECHO 없음·빈 값). 전에는 모든 환경에서 echo=True 라 SQL 과 바인드 값이 서버 로그에 남았다.
      - 켜도 바인드 값은 로그에 나오지 않는다(hide_parameters). 이메일·비밀번호 해시·사용자가 쓴 글이 바인드 값이다.
      - 앱 엔진과 워커 엔진이 같은 옵션을 쓴다.
"""

REQUIRED = {
    "postgres_host": "db",
    "postgres_port": 5432,
    "postgres_user": "app_login",
    "postgres_password": "app_pw",
    "postgres_db": "glowship",
    "jwt_secret_key": "x" * 32,
}
SECRET = "lead@a-beauty.example"


def _settings(monkeypatch, **overrides) -> Settings:
    monkeypatch.delenv("DB_ECHO", raising=False)
    return Settings(_env_file=None, **(REQUIRED | overrides))


@pytest.mark.parametrize("overrides", [{}, {"db_echo": ""}, {"db_echo": "  "}])
def test_db_echo_is_off_by_default_or_blank(monkeypatch, overrides):
    assert _settings(monkeypatch, **overrides).db_echo is False


def test_db_echo_can_be_turned_on(monkeypatch):
    assert _settings(monkeypatch, db_echo="true").db_echo is True


def test_db_echo_rejects_other_values(monkeypatch):
    with pytest.raises(ValidationError):
        _settings(monkeypatch, db_echo="verbose")


@pytest.mark.parametrize("db_echo", [False, True])
def test_options_always_hide_parameters(db_echo):
    assert database.sql_log_options(db_echo) == {
        "echo": db_echo,
        "hide_parameters": True,
    }


def test_app_and_worker_engines_follow_setting():
    for engine in (database.engine, database.worker_engine):
        assert engine.sync_engine.echo is get_settings().db_echo
        assert engine.sync_engine.hide_parameters is True


def _run_statement_with_secret(db_echo: bool) -> None:
    engine = create_engine("sqlite://", **database.sql_log_options(db_echo))
    with engine.connect() as connection:
        connection.execute(text("SELECT :email"), {"email": SECRET})
    engine.dispose()


def test_echo_on_logs_sql_without_bind_values(caplog):
    with caplog.at_level(logging.INFO, logger="sqlalchemy.engine"):
        _run_statement_with_secret(db_echo=True)

    assert "SELECT ?" in caplog.text
    assert SECRET not in caplog.text
