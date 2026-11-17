import base64
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.exceptions import ForbiddenException
from app.core.rls_context import set_rls_context
from app.core.security.auth import CurrentUser
from app.core.security.hashing import verify_password
from app.modules.user.application.client_account_service import ClientAccountService
from app.modules.user.application.exception.user_exceptions import (
    UserNotFoundException,
)
from app.modules.user.infrastructure.repository.user_repository import UserRepository

"""
    Client 계정 비밀번호 재설정 (POST /admin/users/{userId}/password, C47) — 실제 DB, 끝나면 롤백
      - 앱 DB 역할(glowship_v4_app)로 바꾸고 app.user_id 만 넣는다(재설정 API 와 같은 상태, 고객사 컨텍스트 없음).
      - DB 함수(0012)가 계정 관리 권한이 있는 배정이면 해시를 바꾸고, 권한 없는 배정은 42501, 담당 고객사의
        Client 계정이 아니면 P0002 를 던진다. 실패한 호출은 해시를 바꾸지 않는다.
      - 로컬 개발 DB 는 superuser 라 SET LOCAL ROLE 로 정책을 적용한다. 0012 가 없거나 해당 배정이 없는 DB 면 확인하지 않는다.
"""

NEW_HASH = "$2b$12$" + "a" * 53
_FUNCTION_EXISTS = text(
    "SELECT EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'app_admin_reset_client_user_password')"
)
# ACTIVE ADMIN 의 배정 한 건과 그 고객사의 Client 계정 한 명
_FIND_TARGET = text(
    "SELECT a.admin_user_id, m.user_id, m.email FROM admin_assignments a"
    " JOIN users u ON u.user_id = a.admin_user_id AND u.role_code = 'ADMIN' AND u.status = 'ACTIVE'"
    " JOIN users m ON m.company_id = a.company_id"
    " WHERE a.can_manage_client_accounts = :can_manage"
    " ORDER BY a.admin_user_id, m.user_id LIMIT 1"
)
# 그 ADMIN 이 배정되지 않은 고객사의 Client 계정
_FIND_UNASSIGNED_TARGET = text(
    "SELECT u.user_id AS admin_user_id, m.user_id, m.email FROM users u"
    " JOIN users m ON m.company_id IS NOT NULL"
    " WHERE u.role_code = 'ADMIN' AND u.status = 'ACTIVE'"
    " AND NOT EXISTS (SELECT 1 FROM admin_assignments a"
    "                  WHERE a.admin_user_id = u.user_id AND a.company_id = m.company_id)"
    " ORDER BY u.user_id, m.user_id LIMIT 1"
)
_HASH_OF = text("SELECT password_hash FROM users WHERE user_id = :user_id")


async def _require_function(db) -> None:
    if not await db.scalar(_FUNCTION_EXISTS):
        pytest.skip("마이그레이션 0012 가 적용되지 않은 DB 라 건너뜀")


async def _reset_as_app_role(db, *, actor_id, user_id, password_hash=NEW_HASH):
    """
    앱 DB 역할로 함수를 부르고 superuser 로 돌아온다.
    실패하면 SAVEPOINT 를 되돌려 다음 조회가 되게 한다. 되돌리면 SAVEPOINT 뒤에 바꾼 역할도 함께 풀린다.
    """
    async with db.begin_nested():
        await db.execute(text("SET LOCAL ROLE glowship_v4_app"))
        await set_rls_context(db, user_id=actor_id, company_id=None)
        await UserRepository(db).reset_client_user_password(
            user_id=user_id, password_hash=password_hash
        )
        await db.execute(text("RESET ROLE"))


async def _pgcode_of_reset(db, **kwargs) -> str | None:
    try:
        await _reset_as_app_role(db, **kwargs)
    except DBAPIError as error:
        assert await db.scalar(text("SELECT current_user")) != "glowship_v4_app"
        return getattr(error.orig, "pgcode", None) or error.orig.sqlstate
    return None


async def test_managing_admin_resets_password_under_app_role(db_write_session):
    db = db_write_session
    await _require_function(db)
    target = (await db.execute(_FIND_TARGET, {"can_manage": True})).one_or_none()
    if target is None:
        return

    await _reset_as_app_role(db, actor_id=target.admin_user_id, user_id=target.user_id)

    assert await db.scalar(_HASH_OF, {"user_id": target.user_id}) == NEW_HASH
    # 로그인 조회가 새 해시를 본다. 계정 상태·역할은 그대로다.
    login_user = await UserRepository(db).find_login_user(target.email)
    assert login_user.password_hash == NEW_HASH


async def test_assignment_without_manage_permission_is_42501(db_write_session):
    db = db_write_session
    await _require_function(db)
    target = (await db.execute(_FIND_TARGET, {"can_manage": False})).one_or_none()
    if target is None:
        return
    before = await db.scalar(_HASH_OF, {"user_id": target.user_id})

    pgcode = await _pgcode_of_reset(
        db, actor_id=target.admin_user_id, user_id=target.user_id
    )

    assert pgcode == "42501"
    assert await db.scalar(_HASH_OF, {"user_id": target.user_id}) == before


async def test_targets_outside_assigned_client_users_are_P0002(db_write_session):
    db = db_write_session
    await _require_function(db)
    target = (await db.execute(_FIND_TARGET, {"can_manage": True})).one_or_none()
    if target is None:
        return
    admin_id = target.admin_user_id

    # 없는 계정
    assert (
        await _pgcode_of_reset(db, actor_id=admin_id, user_id=uuid.uuid7()) == "P0002"
    )
    # Client 역할이 아닌 계정 (자기 자신)
    assert await _pgcode_of_reset(db, actor_id=admin_id, user_id=admin_id) == "P0002"
    # 배정되지 않은 고객사의 계정
    unassigned = (await db.execute(_FIND_UNASSIGNED_TARGET)).one_or_none()
    if unassigned is not None:
        before = await db.scalar(_HASH_OF, {"user_id": unassigned.user_id})
        pgcode = await _pgcode_of_reset(
            db, actor_id=unassigned.admin_user_id, user_id=unassigned.user_id
        )
        assert pgcode == "P0002"
        assert await db.scalar(_HASH_OF, {"user_id": unassigned.user_id}) == before


async def test_non_admin_actor_and_non_bcrypt_hash_are_rejected(db_write_session):
    db = db_write_session
    await _require_function(db)
    target = (await db.execute(_FIND_TARGET, {"can_manage": True})).one_or_none()
    if target is None:
        return
    before = await db.scalar(_HASH_OF, {"user_id": target.user_id})

    # Client 계정이 자기 비밀번호를 이 함수로 바꿀 수 없다
    assert (
        await _pgcode_of_reset(db, actor_id=target.user_id, user_id=target.user_id)
        == "42501"
    )
    # 컨텍스트가 없는 호출
    assert await _pgcode_of_reset(db, actor_id=None, user_id=target.user_id) == "42501"
    # 평문이 해시 자리에 들어오면 저장하지 않는다
    assert (
        await _pgcode_of_reset(
            db,
            actor_id=target.admin_user_id,
            user_id=target.user_id,
            password_hash="plain-password",
        )
        == "22023"
    )
    assert await db.scalar(_HASH_OF, {"user_id": target.user_id}) == before


async def test_service_maps_real_db_errors_and_stores_verifiable_hash(db_write_session):
    """서비스부터 DB 함수까지 실제로 잇는다. 실제 bcrypt 해시가 함수의 형식 검사를 통과하고, 드라이버 오류의 SQLSTATE 가 404·403 으로 바뀐다."""
    db = db_write_session
    await _require_function(db)
    target = (await db.execute(_FIND_TARGET, {"can_manage": True})).one_or_none()
    if target is None:
        return
    service = ClientAccountService(
        db=db,
        user_repo=UserRepository(db),
        company_repo=None,
        admin_assignment_repo=None,
    )
    actor = CurrentUser(
        user_id=target.admin_user_id, role_code="ADMIN", company_id=None
    )
    encoded = base64.b64encode(b"newpass123").decode()

    await service.reset_client_user_password(
        actor=actor, user_id=target.user_id, encoded_password=encoded
    )

    stored = await db.scalar(_HASH_OF, {"user_id": target.user_id})
    assert await verify_password("newpass123", stored)

    with pytest.raises(UserNotFoundException):
        async with db.begin_nested():
            await service.reset_client_user_password(
                actor=actor, user_id=uuid.uuid7(), encoded_password=encoded
            )
    no_manage = (await db.execute(_FIND_TARGET, {"can_manage": False})).one_or_none()
    if no_manage is not None:
        no_manage_actor = CurrentUser(
            user_id=no_manage.admin_user_id, role_code="ADMIN", company_id=None
        )
        with pytest.raises(ForbiddenException):
            async with db.begin_nested():
                await service.reset_client_user_password(
                    actor=no_manage_actor,
                    user_id=no_manage.user_id,
                    encoded_password=encoded,
                )
