import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database
from app.core.config import Settings, get_settings

"""
    수집 적재 전용 워커 DB 연결 (GLOW-13, API 25)
      - 설정: WORKER_POSTGRES_USER·PASSWORD 는 함께 넣거나 함께 비운다. 비우면 앱 연결과 같다.
      - 세션: open_worker_session 은 워커 엔진에 묶인 별도 세션이고 commit 하지 않는다.
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


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **(REQUIRED | overrides))


def test_without_worker_account_uses_app_connection():
    settings = _settings()

    assert settings.uses_separate_worker_account is False
    assert settings.worker_database_url == settings.database_url
    assert (
        settings.database_url
        == "postgresql+asyncpg://app_login:app_pw@db:5432/glowship"
    )


def test_worker_account_changes_only_credentials():
    settings = _settings(
        worker_postgres_user="worker_login", worker_postgres_password="worker_pw"
    )

    assert settings.uses_separate_worker_account is True
    assert (
        settings.worker_database_url
        == "postgresql+asyncpg://worker_login:worker_pw@db:5432/glowship"
    )
    assert settings.database_url.startswith("postgresql+asyncpg://app_login:")


def test_blank_worker_values_mean_unset():
    settings = _settings(worker_postgres_user="  ", worker_postgres_password="")

    assert settings.uses_separate_worker_account is False
    assert settings.worker_database_url == settings.database_url


@pytest.mark.parametrize(
    "partial",
    [
        {"worker_postgres_user": "worker_login"},
        {"worker_postgres_password": "worker_pw"},
        {"worker_postgres_user": "worker_login", "worker_postgres_password": " "},
    ],
)
def test_half_configured_worker_account_fails_at_startup(partial):
    """계정·비밀번호 하나만 있으면 운영에서 조용히 앱 계정으로 붙지 않도록 기동 시점에 실패"""
    with pytest.raises(ValidationError, match="WORKER_POSTGRES_USER"):
        _settings(**partial)


def test_worker_engine_pins_worker_role_at_connect(monkeypatch):
    """RLS 가 역할 이름(current_user)을 비교하므로 연결 단계에서 워커 역할로 바꾼다. 다른 connect_args 는 그대로 둔다"""
    captured = {}

    def fake_create_async_engine(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return "engine"

    monkeypatch.setattr(database, "create_async_engine", fake_create_async_engine)

    result = database.create_worker_engine(
        "postgresql+asyncpg://worker_login:pw@db:5432/glowship",
        echo=True,
        connect_args={"timeout": 2},
    )

    assert result == "engine"
    assert captured["connect_args"] == {
        "timeout": 2,
        "server_settings": {"role": "glowship_v4_worker"},
    }
    assert captured["echo"] is True
    assert database.WORKER_DB_ROLE == "glowship_v4_worker"


def test_worker_engine_follows_settings():
    settings = get_settings()
    if settings.uses_separate_worker_account:
        assert database.worker_engine is not database.engine
        assert database.worker_engine.url.username == settings.worker_postgres_user
    else:
        # 워커 계정이 없으면 커넥션 풀을 하나 더 만들지 않고 앱 엔진을 그대로 쓴다
        assert database.worker_engine is database.engine
    assert database.WorkerSessionLocal.kw["bind"] is database.worker_engine


async def test_open_worker_session_is_a_separate_uncommitted_session():
    async with database.open_worker_session() as session:
        assert isinstance(session, AsyncSession)
        assert session.bind is database.worker_engine
        # DB 에 붙지 않으므로 트랜잭션도 시작하지 않는다 (수집 서비스가 직접 commit·rollback)
        assert not session.in_transaction()
