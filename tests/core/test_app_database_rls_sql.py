import os
import secrets
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import APP_DB_ROLE
from app.core.database import create_app_engine
from app.core.rls_context import set_rls_context
from app.modules.user.infrastructure.repository.user_repository import UserRepository
from tests.core.test_worker_database_rls_sql import _admin_engine, _login_url

"""
    앱 로그인 계정으로 붙었을 때 로그인·내 정보·고객사 데이터가 되는지 실제 DB 로 확인 (이슈 #41)
      - 운영은 앱 역할(glowship_v4_app)에 속한 로그인 계정으로 붙는다. 로컬 개발 DB 는 superuser 라 RLS 를 우회하므로,
        시험 안에서 앱 역할에 속한 임시 로그인 계정을 만들어 그 계정으로 붙는다(워커 시험 test_worker_database_rls_sql 와 같은 방식).
      - 순서는 요청과 같다: ① 로그인(컨텍스트 없음, DB 함수 0008) ② 컨텍스트를 넣고 내 정보(users·companies)
        ③ 고객사 데이터(skus). 역할을 고정하지 않은 연결은 컨텍스트가 있어도 0행이다.
      - 운영자 회원가입(users INSERT)은 앱 역할 연결에서 권한 오류다(이슈 #78). 회원가입 API 는 운영에 등록하지 않는다.
      - 임시 계정은 10분 뒤 비밀번호 만료, 끝나면 지운다. 다음 실행 시작 때 남은 계정(tmp_app_login_*)을 먼저 지운다.
        읽기와 거부되는 INSERT 뿐이라 DB 는 바뀌지 않는다.
      - superuser 가 아니거나 DB 에 붙을 수 없으면 건너뛴다. SKIP_DB_ROLE_TESTS=1 이면 건너뛴다.
"""

TMP_ROLE_PREFIX = "tmp_app_login_"


@pytest.fixture
async def app_login() -> AsyncGenerator[tuple[str, str]]:
    """앱 역할에 속한 임시 로그인 계정 (이름, 비밀번호)"""
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
                text(
                    "SELECT count(*) = 1 FROM pg_proc WHERE proname = 'app_auth_find_login_user'"
                )
            ):
                pytest.skip("로그인 조회 함수(0008)가 없는 DB 라 건너뜀")
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
            await conn.execute(text(f"GRANT {APP_DB_ROLE} TO {name}"))
    except (OSError, SQLAlchemyError) as error:
        await admin.dispose()
        pytest.skip(f"DB 에 접속할 수 없어 건너뜀: {type(error).__name__}")
    try:
        yield name, password
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f"DROP ROLE IF EXISTS {name}"))
        await admin.dispose()


async def _client_member() -> tuple[str, uuid.UUID, uuid.UUID]:
    """(이메일, 사용자 ID, 고객사 ID) — ACTIVE 고객사의 ACTIVE 팀원"""
    admin = _admin_engine()
    try:
        async with admin.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT u.email, u.user_id, u.company_id FROM users u"
                        " JOIN companies c USING (company_id)"
                        " WHERE u.role_code = 'CLIENT_MEMBER' AND u.status = 'ACTIVE'"
                        " AND c.status = 'ACTIVE' ORDER BY u.email LIMIT 1"
                    )
                )
            ).one_or_none()
    finally:
        await admin.dispose()
    if row is None:
        pytest.skip("ACTIVE 팀원이 없는 DB 라 건너뜀")
    return row.email, row.user_id, row.company_id


async def _request_flow(
    engine: AsyncEngine, email: str, user_id: uuid.UUID, company_id: uuid.UUID
) -> tuple[str, bool, bool, int, int]:
    """(current_user, 로그인 조회 성공, 내 정보 조회 성공, 보이는 고객사 수, 보이는 SKU 수). 읽기만 한다"""
    async with AsyncSession(bind=engine) as session:
        try:
            who = await session.scalar(text("SELECT current_user"))
            repo = UserRepository(session)
            login = await repo.find_login_user(email)  # ① 컨텍스트 없음
            await set_rls_context(session, user_id=user_id, company_id=company_id)
            me = await repo.find_by_id(user_id)  # ② 내 정보
            companies = await session.scalar(text("SELECT count(*) FROM companies"))
            skus = await session.scalar(text("SELECT count(*) FROM skus"))  # ③
            return (
                who,
                login is not None and login.user_id == user_id,
                me is not None,
                companies,
                skus,
            )
        finally:
            await session.rollback()


async def test_app_login_logs_in_and_sees_own_company_only_with_role_pinned(
    app_login,
):
    name, password = app_login
    email, user_id, company_id = await _client_member()
    url = _login_url(name, password)
    pinned = create_app_engine(
        url, APP_DB_ROLE, poolclass=NullPool, connect_args={"timeout": 2}
    )
    plain = create_async_engine(url, poolclass=NullPool, connect_args={"timeout": 2})
    try:
        who, logged_in, me, companies, skus = await _request_flow(
            pinned, email, user_id, company_id
        )
        assert (who, logged_in, me, companies) == (APP_DB_ROLE, True, True, 1)
        assert skus > 0

        # 수정 전 연결(역할 고정 없음): current_user 가 로그인 계정 이름이라 app_context_allowed(current_user) 가 거짓.
        # 본인 행(users_select_tenant_or_self 의 user_id 조건)만 보이고 고객사 데이터는 모두 0행이다.
        who, _, me, companies, skus = await _request_flow(
            plain, email, user_id, company_id
        )
        assert (who, me, companies, skus) == (name, True, 0, 0)
    finally:
        await pinned.dispose()
        await plain.dispose()


async def test_app_login_cannot_insert_users_with_role_pinned(app_login):
    """운영자 회원가입(create_admin_user)은 앱 역할 연결에서 권한 오류다 (이슈 #78)"""
    name, password = app_login
    pinned = create_app_engine(
        _login_url(name, password),
        APP_DB_ROLE,
        poolclass=NullPool,
        connect_args={"timeout": 2},
    )
    try:
        async with AsyncSession(bind=pinned) as session:
            try:
                with pytest.raises(ProgrammingError) as raised:
                    await UserRepository(session).create_admin_user(
                        email=f"{name}@example.com", password_hash="x"
                    )
            finally:
                await session.rollback()
    finally:
        await pinned.dispose()
    # 42501 insufficient_privilege: 앱 역할에는 users INSERT 권한이 없다(0001). 운영 계정은 DB 에 직접 넣는다(README)
    assert raised.value.orig.sqlstate == "42501"
