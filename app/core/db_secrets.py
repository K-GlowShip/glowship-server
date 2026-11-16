"""Read rotating RDS credentials on new connections, never into logs or env files."""

import asyncio
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import asyncpg
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


class DatabaseSecretError(RuntimeError):
    """Only fixed error codes: SDK errors/secret contents must not reach logs."""


@dataclass(frozen=True, repr=False)
class DatabaseCredential:
    username: str
    password: str = field(repr=False)


class RdsCredentialProvider:
    def __init__(
        self,
        secret_arn: str,
        *,
        username: str,
        host: str,
        port: int,
        database: str,
        ttl_seconds: float = 300,
        client_factory: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        parts = secret_arn.split(":", 5)
        if (
            len(parts) != 6
            or parts[0] != "arn"
            or parts[2] != "secretsmanager"
            or not parts[3]
            or not parts[5].startswith("secret:")
        ):
            raise DatabaseSecretError("DB_SECRET_ARN_INVALID")
        self._secret_arn = secret_arn
        self._region = parts[3]
        self._username = username
        self._host = host
        self._port = port
        self._database = database
        self._ttl_seconds = ttl_seconds
        self._client_factory = client_factory
        self._clock = clock
        self._client: Any = None
        self._credential: DatabaseCredential | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def _new_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        # Imported/constructed in a worker thread, only when this mode is enabled.
        import boto3
        from botocore.config import Config

        return boto3.client(
            "secretsmanager",
            region_name=self._region,
            config=Config(
                connect_timeout=2,
                read_timeout=2,
                retries={"mode": "standard", "total_max_attempts": 2},
            ),
        )

    def _read(self, rejected: DatabaseCredential | None) -> DatabaseCredential:
        with self._lock:
            # Another connection may already have refreshed the rejected value.
            if self._credential is not None and (
                (rejected is None and self._clock() < self._expires_at)
                or (rejected is not None and self._credential is not rejected)
            ):
                return self._credential
            try:
                if self._client is None:
                    self._client = self._new_client()
                response = self._client.get_secret_value(
                    SecretId=self._secret_arn, VersionStage="AWSCURRENT"
                )
                value = json.loads(response["SecretString"])
            except Exception:  # noqa: BLE001 - redact arbitrary SDK/JSON details
                # Do not fall back to the stale environment password.
                raise DatabaseSecretError("DB_SECRET_READ_FAILED") from None
            if not isinstance(value, dict):
                raise DatabaseSecretError("DB_SECRET_FORMAT_INVALID")
            password = value.get("password")
            if not isinstance(password, str) or not password:
                raise DatabaseSecretError("DB_SECRET_FORMAT_INVALID")
            if value.get("username") != self._username:
                raise DatabaseSecretError("DB_SECRET_TARGET_MISMATCH")
            # RDS-managed secrets may only contain username/password. If target
            # fields are present, they must match the explicit server target.
            for key, expected in (
                ("host", self._host),
                ("port", self._port),
                ("dbname", self._database),
            ):
                if key in value and str(value[key]) != str(expected):
                    raise DatabaseSecretError("DB_SECRET_TARGET_MISMATCH")
            self._credential = DatabaseCredential(self._username, password)
            self._expires_at = self._clock() + self._ttl_seconds
            return self._credential

    async def get(
        self, *, rejected: DatabaseCredential | None = None
    ) -> DatabaseCredential:
        return await asyncio.to_thread(self._read, rejected)


def create_database_engine(
    url: str, secret_arn: str | None, **kwargs: Any
) -> AsyncEngine:
    """Engine for code outside the app engine (scripts/): secret mode or static.

    Settings.database_url carries no password in secret mode, so building an
    engine from that URL directly fails authentication. Seed scripts must come
    through here instead of create_async_engine.
    """
    if secret_arn:
        return create_secret_engine(url, secret_arn, **kwargs)
    return create_async_engine(url, **kwargs)


def create_secret_engine(url: str, secret_arn: str, **kwargs: Any) -> AsyncEngine:
    """App and Alembic share this factory; no DB credentials are persisted."""
    target = make_url(url)
    if target.drivername != "postgresql+asyncpg" or not target.username:
        raise DatabaseSecretError("DB_SECRET_DRIVER_INVALID")
    connect_args = dict(kwargs.pop("connect_args", {}))
    if "password" in connect_args or "user" in connect_args:
        raise DatabaseSecretError("DB_SECRET_CREDENTIAL_OVERRIDE")
    # Use the dialect's URL conversion so ssl/timeouts/query options retain
    # their normal SQLAlchemy meaning. The URL never contains the live secret.
    dialect = target.get_dialect()()
    positional, connection_args = dialect.create_connect_args(target)
    connection_args.update(connect_args)
    connection_args.pop("password", None)
    provider = RdsCredentialProvider(
        secret_arn,
        username=target.username,
        host=target.host or "localhost",
        port=target.port or 5432,
        database=target.database or "",
    )

    async def connect() -> Any:
        credential = await provider.get()
        try:
            return await asyncpg.connect(
                *positional, **connection_args, password=credential.password
            )
        except asyncpg.InvalidPasswordError:
            # Rotation can occur before the cache TTL. Refresh exactly once;
            # never retry queries, migrations, or failed transactions.
            credential = await provider.get(rejected=credential)
            return await asyncpg.connect(
                *positional, **connection_args, password=credential.password
            )

    kwargs.setdefault("pool_pre_ping", True)
    return create_async_engine(url, async_creator=connect, **kwargs)
