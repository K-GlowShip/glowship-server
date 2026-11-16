import pytest
from sqlalchemy import text

from app.core.rls_context import set_rls_context
from app.modules.company.infrastructure.repository.admin_assignment_repository import (
    AdminAssignmentRepository,
    AssignedCompany,
)

"""
    담당 고객사 목록 조회 (GET /me assignedCompanies[], C47) — 실제 DB, 끝나면 롤백
      - 앱 DB 역할(glowship_v4_app)로 바꾸고 app.user_id 만 넣는다(고객사를 고르기 전, GET /me 와 같은 상태).
      - find_assigned_companies(DB 함수 0011)는 ADMIN 에게 배정된 ACTIVE 고객사를, OWNER 에게 전체 ACTIVE 고객사를
        돌려주고, select(AdminAssignment)는 app.company_id 가 없어 0행이다.
      - 로컬 개발 DB 는 superuser 라 SET LOCAL ROLE 로 정책을 적용한다. 0011 이 없거나 해당 사용자가 없는 DB 면 확인하지 않는다.
"""

_FUNCTION_EXISTS = text(
    "SELECT EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'app_me_list_assigned_companies')"
)
_FIND_USER = text(
    "SELECT user_id FROM users WHERE role_code = :role_code AND status = 'ACTIVE'"
    " ORDER BY user_id LIMIT 1"
)
# 배정이 있는 ADMIN 을 먼저 고른다. 배정이 하나도 없는 DB 면 ADMIN 아무나(빈 목록 확인).
_FIND_ADMIN = text(
    "SELECT u.user_id FROM users u"
    " LEFT JOIN admin_assignments a ON a.admin_user_id = u.user_id"
    " WHERE u.role_code = 'ADMIN' AND u.status = 'ACTIVE'"
    " GROUP BY u.user_id ORDER BY count(a.admin_assignment_id) DESC, u.user_id LIMIT 1"
)
_EXPECTED_FOR_ADMIN = text(
    "SELECT c.company_id, c.company_name, a.can_manage_client_accounts"
    " FROM admin_assignments a JOIN companies c ON c.company_id = a.company_id"
    " WHERE a.admin_user_id = :user_id AND c.status = 'ACTIVE'"
    " ORDER BY c.company_name, c.company_id"
)
_EXPECTED_FOR_OWNER = text(
    "SELECT company_id, company_name, true AS can_manage_client_accounts"
    " FROM companies WHERE status = 'ACTIVE' ORDER BY company_name, company_id"
)


async def _require_function(db) -> None:
    if not await db.scalar(_FUNCTION_EXISTS):
        pytest.skip("마이그레이션 0011 이 적용되지 않은 DB 라 건너뜀")


async def _expected(db, stmt, params=None) -> list[AssignedCompany]:
    rows = await db.execute(stmt, params or {})
    return [AssignedCompany(*row) for row in rows]


async def _as_app_role(db, user_id) -> AdminAssignmentRepository:
    await db.execute(text("SET LOCAL ROLE glowship_v4_app"))
    await set_rls_context(db, user_id=user_id, company_id=None)
    return AdminAssignmentRepository(db)


async def test_admin_gets_assigned_active_companies_under_app_role(db_write_session):
    db = db_write_session
    await _require_function(db)
    admin_id = await db.scalar(_FIND_ADMIN)
    if admin_id is None:
        return
    expected = await _expected(db, _EXPECTED_FOR_ADMIN, {"user_id": admin_id})
    repo = await _as_app_role(db, admin_id)

    assert await repo.find_assigned_companies() == expected
    # 고객사를 고르지 않은 상태에서 정책만으로는 배정이 보이지 않는다 (함수가 필요한 이유)
    for company in expected:
        assert (
            await repo.find_by_admin_and_company(admin_id, company.company_id) is None
        )


async def test_inactive_company_is_left_out_under_app_role(db_write_session):
    db = db_write_session
    await _require_function(db)
    admin_id = await db.scalar(_FIND_ADMIN)
    before = (
        await _expected(db, _EXPECTED_FOR_ADMIN, {"user_id": admin_id})
        if admin_id is not None
        else []
    )
    if not before:
        return
    inactive = before[0]
    await db.execute(
        text("UPDATE companies SET status = 'INACTIVE' WHERE company_id = :company_id"),
        {"company_id": inactive.company_id},
    )
    repo = await _as_app_role(db, admin_id)

    assert await repo.find_assigned_companies() == before[1:]


async def test_owner_gets_all_active_companies_under_app_role(db_write_session):
    db = db_write_session
    await _require_function(db)
    owner_id = await db.scalar(_FIND_USER, {"role_code": "OWNER"})
    if owner_id is None:
        return
    expected = await _expected(db, _EXPECTED_FOR_OWNER)
    repo = await _as_app_role(db, owner_id)

    found = await repo.find_assigned_companies()

    assert found == expected
    assert all(company.can_manage_client_accounts for company in found)


async def test_client_user_and_missing_context_get_nothing(db_write_session):
    db = db_write_session
    await _require_function(db)
    client_id = await db.scalar(_FIND_USER, {"role_code": "CLIENT_MEMBER"})
    repo = await _as_app_role(db, client_id)

    if client_id is not None:
        assert await repo.find_assigned_companies() == []
    await set_rls_context(db, user_id=None, company_id=None)
    assert await repo.find_assigned_companies() == []
