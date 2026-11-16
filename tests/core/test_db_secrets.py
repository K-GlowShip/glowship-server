import asyncio
import json
from unittest.mock import Mock

import asyncpg
import pytest

from app.core import db_secrets
from app.core.config import Settings

ARN = "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:test-db-abcdef"
TARGET = {"username": "postgres", "host": "db", "port": 5432, "database": "glowship"}
URL = "postgresql+asyncpg://postgres:@db:5432/glowship"


def response(password="old", **extra):
    return {
        "SecretString": json.dumps(
            {"username": "postgres", "password": password, **extra}
        )
    }


def provider(client, **extra):
    return db_secrets.RdsCredentialProvider(
        ARN, **TARGET, client_factory=lambda: client, **extra
    )


async def test_cache_avoids_an_aws_request_per_connection_and_refreshes_after_ttl():
    now = [0.0]
    client = Mock()
    client.get_secret_value.side_effect = [response(), response("new")]
    source = provider(client, clock=lambda: now[0], ttl_seconds=10)
    first = await source.get()
    assert (await source.get()) is first
    now[0] = 11
    assert (await source.get()).password == "new"
    assert client.get_secret_value.call_count == 2
    client.get_secret_value.assert_called_with(SecretId=ARN, VersionStage="AWSCURRENT")


async def test_concurrent_rejection_of_one_cached_value_only_refreshes_once():
    client = Mock()
    client.get_secret_value.side_effect = [response(), response("new")]
    source = provider(client)
    old = await source.get()
    refreshed = await asyncio.gather(*(source.get(rejected=old) for _ in range(5)))
    assert all(value.password == "new" for value in refreshed)
    assert client.get_secret_value.call_count == 2


async def test_sdk_runs_off_event_loop_and_failure_does_not_expose_secret():
    client = Mock()

    def fail(**kwargs):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        raise ValueError("SECRET_PASSWORD_DO_NOT_LOG")

    client.get_secret_value.side_effect = fail
    with pytest.raises(db_secrets.DatabaseSecretError) as raised:
        await provider(client).get()
    assert str(raised.value) == "DB_SECRET_READ_FAILED"
    assert raised.value.__suppress_context__


@pytest.mark.parametrize(
    "payload",
    [
        response(username="other"),
        response(host="other-db"),
        response(port=9999),
        response(dbname="other"),
        response(password=""),
        {"SecretString": "[]"},
        {"SecretString": "not-json"},
    ],
)
async def test_wrong_target_or_invalid_secret_fails_without_static_fallback(payload):
    client = Mock()
    client.get_secret_value.return_value = payload
    with pytest.raises(db_secrets.DatabaseSecretError):
        await provider(client).get()


def capture_creator(monkeypatch, credentials):
    client = Mock()
    client.get_secret_value.side_effect = [response(p) for p in credentials]
    real_provider = db_secrets.RdsCredentialProvider
    monkeypatch.setattr(
        db_secrets,
        "RdsCredentialProvider",
        lambda *args, **kwargs: real_provider(
            *args, **kwargs, client_factory=lambda: client
        ),
    )
    captured = {}

    def create(url, **kwargs):
        captured.update(kwargs)
        return "engine"

    monkeypatch.setattr(db_secrets, "create_async_engine", create)
    assert (
        db_secrets.create_secret_engine(
            URL,
            ARN,
            connect_args={"timeout": 2, "server_settings": {"role": "app_role"}},
        )
        == "engine"
    )
    assert captured["pool_pre_ping"] is True
    return captured["async_creator"], client


async def test_rotation_retries_connection_once_with_new_password_and_keeps_role(
    monkeypatch,
):
    creator, client = capture_creator(monkeypatch, ["old", "new"])
    seen = []

    async def connect(**kwargs):
        seen.append(kwargs)
        if kwargs["password"] == "old":
            raise asyncpg.InvalidPasswordError("rotated")
        return "connection"

    monkeypatch.setattr(db_secrets.asyncpg, "connect", connect)
    assert await creator() == "connection"
    assert [call["password"] for call in seen] == ["old", "new"]
    assert all(call["server_settings"] == {"role": "app_role"} for call in seen)
    assert all(call["timeout"] == 2 for call in seen)
    assert client.get_secret_value.call_count == 2


@pytest.mark.parametrize("error_type", [asyncpg.InvalidPasswordError, OSError])
async def test_connection_retries_are_bounded_and_network_errors_do_not_refresh(
    monkeypatch, error_type
):
    creator, client = capture_creator(monkeypatch, ["old", "new"])
    calls = []

    async def connect(**kwargs):
        calls.append(kwargs)
        raise error_type("failure")

    monkeypatch.setattr(db_secrets.asyncpg, "connect", connect)
    with pytest.raises(error_type):
        await creator()
    expected = 2 if error_type is asyncpg.InvalidPasswordError else 1
    assert len(calls) == expected
    assert client.get_secret_value.call_count == expected


def settings(**overrides):
    return Settings(
        _env_file=None,
        **(
            {
                "app_env": "dev",
                "postgres_host": "db",
                "postgres_port": 5432,
                "postgres_user": "postgres",
                "postgres_db": "glowship",
                "jwt_secret_key": "x" * 32,
            }
            | overrides
        ),
    )


def test_secret_mode_needs_no_static_password_and_does_not_put_it_in_url():
    configured = settings(postgres_secret_arn=ARN, postgres_password="stale-value")
    assert "stale-value" not in configured.database_url
    assert settings(postgres_secret_arn=ARN).database_url == configured.database_url


def test_static_password_reserved_characters_round_trip():
    from sqlalchemy.engine import make_url

    password = "p@ss:/?#%[]"
    assert (
        make_url(settings(postgres_password=password).database_url).password == password
    )


def test_app_factory_preserves_role_when_secret_mode_is_enabled(monkeypatch):
    from app.core import database

    captured = {}

    def create(url, secret_arn, **kwargs):
        captured.update(kwargs)
        assert secret_arn == ARN
        return "engine"

    monkeypatch.setattr(database, "create_secret_engine", create)
    assert (
        database.create_app_engine(
            URL, "glowship_v4_app", secret_arn=ARN, connect_args={"timeout": 2}
        )
        == "engine"
    )
    assert captured["connect_args"] == {
        "timeout": 2,
        "server_settings": {"role": "glowship_v4_app"},
    }


async def test_secret_engine_selects_from_real_postgres_when_available(
    monkeypatch, db_session
):
    """CI PostgreSQL checks the real async_creator/driver integration, not AWS."""
    from sqlalchemy import text
    from sqlalchemy.engine import make_url

    from app.core.config import get_settings

    configured = get_settings()
    client = Mock()
    client.get_secret_value.return_value = {
        "SecretString": json.dumps(
            {
                "username": configured.postgres_user,
                "password": configured.postgres_password,
            }
        )
    }
    real_provider = db_secrets.RdsCredentialProvider
    monkeypatch.setattr(
        db_secrets,
        "RdsCredentialProvider",
        lambda *args, **kwargs: real_provider(
            *args, **kwargs, client_factory=lambda: client
        ),
    )
    url = make_url(configured.database_url).set(password="")
    engine = db_secrets.create_secret_engine(
        url.render_as_string(hide_password=False), ARN
    )
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT 1")) == 1
    finally:
        await engine.dispose()
