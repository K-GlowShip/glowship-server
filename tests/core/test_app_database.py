import pytest
from pydantic import ValidationError

from app.core import database
from app.core.config import APP_DB_ROLE, Settings

"""
    앱 DB 연결 역할 고정 (이슈 #41)
      - 설정: POSTGRES_ROLE 은 비우거나 glowship_v4_app. 넣으면 워커 계정도 있어야 한다(수집은 워커 역할).
      - 엔진: 역할이 있으면 연결할 때 그 역할로 바꾼다(asyncpg server_settings role). 없으면 지금처럼 그대로 붙는다.
"""

REQUIRED = {
    "app_env": "local",
    "postgres_host": "db",
    "postgres_port": 5432,
    "postgres_user": "app_login",
    "postgres_password": "app_pw",
    "postgres_db": "glowship",
    "jwt_secret_key": "x" * 32,
}
WORKER = {"worker_postgres_user": "worker_login", "worker_postgres_password": "w"}


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **(REQUIRED | overrides))


@pytest.mark.parametrize("value", [None, "", "  "])
def test_role_is_unset_by_default_or_blank(value):
    assert _settings(postgres_role=value).postgres_role is None


def test_app_role_with_worker_account_is_accepted():
    assert _settings(postgres_role=APP_DB_ROLE, **WORKER).postgres_role == (
        "glowship_v4_app"
    )


@pytest.mark.parametrize(
    "value", ["glowship_v4_worker", "glowship_v4_owner", "postgres"]
)
def test_other_roles_fail_at_startup(value):
    with pytest.raises(ValidationError, match="POSTGRES_ROLE"):
        _settings(postgres_role=value, **WORKER)


def test_app_role_without_worker_account_fails_at_startup():
    """워커 계정이 없으면 수집 세션이 앱 연결(앱 역할)을 써서 행이 숨겨지므로 기동 시점에 막는다"""
    with pytest.raises(ValidationError, match="WORKER_POSTGRES_USER"):
        _settings(postgres_role=APP_DB_ROLE)


def _capture(monkeypatch) -> dict:
    captured: dict = {}

    def fake_create_async_engine(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return "engine"

    monkeypatch.setattr(database, "create_async_engine", fake_create_async_engine)
    return captured


def test_app_engine_pins_role_at_connect(monkeypatch):
    captured = _capture(monkeypatch)

    result = database.create_app_engine(
        "postgresql+asyncpg://app_login:pw@db:5432/glowship",
        APP_DB_ROLE,
        echo=True,
        connect_args={"timeout": 2},
    )

    assert result == "engine"
    assert captured["connect_args"] == {
        "timeout": 2,
        "server_settings": {"role": "glowship_v4_app"},
    }
    assert captured["echo"] is True


def test_app_engine_without_role_connects_as_login(monkeypatch):
    captured = _capture(monkeypatch)

    database.create_app_engine(
        "postgresql+asyncpg://app_login:pw@db:5432/glowship",
        None,
        connect_args={"timeout": 2},
    )

    assert captured["connect_args"] == {"timeout": 2}
