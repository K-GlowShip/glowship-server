import os
import secrets
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.database import WORKER_DB_ROLE, create_worker_engine
from app.core.rls_context import set_rls_context
from app.modules.data_sync.infrastructure.data_sync_enums import (
    ConnectionMethod,
    SourceDomain,
)
from app.modules.data_sync.infrastructure.repository.ingest_repository import (
    IngestRepository,
)

"""
    워커 로그인 계정으로 붙었을 때 RLS 를 통과하는지 실제 DB 로 확인 (PR #40 리뷰 [크리티컬])
      - RLS 정책은 app_context_allowed(current_user) 로 역할 이름을 비교한다(0001). 로컬 개발 DB 는 superuser 라
        RLS 를 우회하므로, 시험 안에서 워커 역할에 속한 임시 로그인 계정을 만들어 그 계정으로 붙는다.
      - 임시 계정은 시험이 끝나면 지운다. 수집 실행 INSERT 는 트랜잭션 안에서만 하고 롤백한다(DB 는 그대로).
      - 역할은 DB 가 아니라 클러스터(컨테이너) 전체 객체라 같은 컨테이너의 다른 DB 에도 보인다(PR #40 rouiv 리뷰).
        시험이 강제로 끊겨 계정이 남아도 쓸 수 없게 비밀번호를 10분 뒤 만료(VALID UNTIL)로 만들고,
        다음 실행 시작 때 남은 임시 계정(tmp_worker_login_*)을 먼저 지운다.
      - superuser 가 아니어서 계정을 만들 수 없거나 DB 에 붙을 수 없으면 건너뛴다(CI 등).
        로그인 계정을 만들고 싶지 않은 환경이면 SKIP_DB_ROLE_TESTS=1 로 건너뛴다.
"""

TMP_ROLE_PREFIX = "tmp_worker_login_"


def _admin_engine() -> AsyncEngine:
    return create_async_engine(
        get_settings().database_url,
        poolclass=NullPool,
        connect_args={"timeout": 2},
        isolation_level="AUTOCOMMIT",
    )


@pytest.fixture
async def worker_login() -> AsyncGenerator[tuple[str, str]]:
    """워커 역할에 속한 임시 로그인 계정 (이름, 비밀번호). 10분 뒤 비밀번호 만료, 끝나면 지운다"""
    if os.environ.get("SKIP_DB_ROLE_TESTS") == "1":
        pytest.skip("SKIP_DB_ROLE_TESTS=1 이라 임시 로그인 계정 시험을 건너뜀")
    name = f"{TMP_ROLE_PREFIX}{uuid.uuid4().hex[:12]}"
    password = secrets.token_hex(16)
    valid_until = (datetime.now(UTC) + timedelta(minutes=10)).strftime(
        "%Y-%m-%d %H:%M:%S+00"
    )
    admin = _admin_engine()
    try:
        async with admin.connect() as conn:
            if not await conn.scalar(
                text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
            ):
                pytest.skip(
                    "superuser 가 아니어서 임시 로그인 계정을 만들 수 없어 건너뜀"
                )
            if not await conn.scalar(
                text("SELECT count(*) = 1 FROM pg_roles WHERE rolname = :r"),
                {"r": WORKER_DB_ROLE},
            ):
                pytest.skip("워커 역할이 없는 DB 라 건너뜀")
            # 지난 실행이 강제로 끊겨 남은 임시 계정을 먼저 지운다
            leftovers = await conn.scalars(
                text("SELECT rolname FROM pg_roles WHERE starts_with(rolname, :p)"),
                {"p": TMP_ROLE_PREFIX},
            )
            for leftover in list(leftovers):
                await conn.execute(text(f'DROP ROLE IF EXISTS "{leftover}"'))
            await conn.execute(
                text(
                    f"CREATE ROLE {name} LOGIN PASSWORD '{password}'"
                    f" VALID UNTIL '{valid_until}'"
                )
            )
    except (OSError, SQLAlchemyError) as error:
        await admin.dispose()
        pytest.skip(f"DB 에 접속할 수 없어 건너뜀: {type(error).__name__}")
    try:
        yield name, password
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f"DROP ROLE IF EXISTS {name}"))
        await admin.dispose()


def _login_url(name: str, password: str) -> str:
    url = make_url(get_settings().database_url).set(username=name, password=password)
    return url.render_as_string(hide_password=False)


async def _grant_worker_role(name: str) -> None:
    admin = _admin_engine()
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f"GRANT {WORKER_DB_ROLE} TO {name}"))
    finally:
        await admin.dispose()


async def _active_company_id() -> uuid.UUID:
    admin = _admin_engine()
    try:
        async with admin.connect() as conn:
            company_id = await conn.scalar(
                text("SELECT company_id FROM companies WHERE status = 'ACTIVE' LIMIT 1")
            )
    finally:
        await admin.dispose()
    if company_id is None:
        pytest.skip("ACTIVE 고객사가 없는 DB 라 건너뜀")
    return company_id


async def _visible_and_insert(
    engine: AsyncEngine, company_id: uuid.UUID
) -> tuple[str, int, bool]:
    """(current_user, 보이는 고객사 행 수, RUNNING 수집 실행 INSERT 성공 여부). 트랜잭션은 롤백한다"""
    async with AsyncSession(bind=engine) as session:
        try:
            await set_rls_context(session, user_id=None, company_id=company_id)
            who = await session.scalar(text("SELECT current_user"))
            visible = await session.scalar(text("SELECT count(*) FROM companies"))
            try:
                async with session.begin_nested():
                    await IngestRepository(session).start_run(
                        company_id,
                        SourceDomain.SHIPMENT,
                        ConnectionMethod.API,
                        datetime(2026, 7, 2, tzinfo=UTC),
                    )
                inserted = True
            except DBAPIError:
                inserted = False
            return who, visible, inserted
        finally:
            await session.rollback()


async def test_worker_login_sees_rows_and_inserts_run_only_with_role_pinned(
    worker_login,
):
    name, password = worker_login
    await _grant_worker_role(name)
    url = _login_url(name, password)
    pinned = create_worker_engine(url, poolclass=NullPool, connect_args={"timeout": 2})
    plain = create_async_engine(url, poolclass=NullPool, connect_args={"timeout": 2})
    try:
        company_id = await _active_company_id()

        # 수정한 연결: 워커 역할로 바뀌어 RLS 를 통과한다
        assert await _visible_and_insert(pinned, company_id) == (
            WORKER_DB_ROLE,
            1,
            True,
        )
        # 수정 전 연결(역할 고정 없음): 로그인 계정 이름이라 행이 숨겨지고 INSERT 는 RLS 위반
        assert await _visible_and_insert(plain, company_id) == (name, 0, False)
    finally:
        await pinned.dispose()
        await plain.dispose()


async def test_login_not_in_worker_role_cannot_connect(worker_login):
    """워커 역할에 속하지 않은 계정을 잘못 넣으면 조용히 붙지 않고 연결이 실패한다"""
    name, password = worker_login
    engine = create_worker_engine(
        _login_url(name, password), poolclass=NullPool, connect_args={"timeout": 2}
    )
    try:
        with pytest.raises(DBAPIError, match="permission denied to set role"):
            async with engine.connect() as conn:
                await conn.scalar(text("SELECT 1"))
    finally:
        await engine.dispose()
