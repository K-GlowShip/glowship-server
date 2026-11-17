from sqlalchemy import text

from app.core.pagination import PageParams
from app.core.rls_context import set_rls_context
from app.modules.user.infrastructure.repository.user_repository import UserRepository
from app.modules.user.infrastructure.user_enums import UserRoleCode, UserStatus

"""
    고객사 계정 목록 조회 (GET /admin/users, C47) — 실제 DB, 끝나면 롤백
      - 앱 DB 역할(glowship_v4_app)로 바꾸고 배정된 ADMIN 과 그 고객사를 컨텍스트에 넣는다(목록 API 와 같은 상태).
      - users 정책(users_select_tenant_or_self)이 그 고객사의 계정을 보여 주므로 DB 함수 없이 select 로 읽는다.
        배정되지 않은 ADMIN 의 컨텍스트에서는 같은 조회가 0행이다.
      - 로컬 개발 DB 는 superuser 라 SET LOCAL ROLE 로 정책을 적용한다. 배정·계정이 없는 DB 면 확인하지 않는다.
"""

# Client 계정이 있는 ACTIVE 고객사에 배정된 ACTIVE ADMIN 한 명
_FIND_ASSIGNMENT = text(
    "SELECT a.admin_user_id, a.company_id FROM admin_assignments a"
    " JOIN users u ON u.user_id = a.admin_user_id AND u.role_code = 'ADMIN' AND u.status = 'ACTIVE'"
    " JOIN companies c ON c.company_id = a.company_id AND c.status = 'ACTIVE'"
    " WHERE EXISTS (SELECT 1 FROM users m WHERE m.company_id = a.company_id)"
    " ORDER BY a.admin_user_id, a.company_id LIMIT 1"
)
_EXPECTED = text(
    "SELECT user_id FROM users"
    " WHERE company_id = :company_id AND role_code IN ('CLIENT_MEMBER', 'CLIENT_LEAD')"
    " ORDER BY created_at DESC, user_id"
)
# 이 고객사에 배정되지 않은 ACTIVE ADMIN
_FIND_UNASSIGNED_ADMIN = text(
    "SELECT u.user_id FROM users u WHERE u.role_code = 'ADMIN' AND u.status = 'ACTIVE'"
    " AND NOT EXISTS (SELECT 1 FROM admin_assignments a"
    "                  WHERE a.admin_user_id = u.user_id AND a.company_id = :company_id)"
    " ORDER BY u.user_id LIMIT 1"
)
ALL = PageParams(page=1, size=100)


async def _page(repo, company_id, *, role_code=None, status=None, params=ALL):
    return await repo.find_client_users_page(
        company_id=company_id, role_code=role_code, status=status, params=params
    )


async def test_assigned_admin_reads_client_users_under_app_role(db_write_session):
    db = db_write_session
    assignment = (await db.execute(_FIND_ASSIGNMENT)).one_or_none()
    if assignment is None:
        return
    admin_id, company_id = assignment
    expected = list(await db.scalars(_EXPECTED, {"company_id": company_id}))
    if len(expected) < 2 or len(expected) > ALL.size:
        return
    await db.execute(text("SET LOCAL ROLE glowship_v4_app"))
    await set_rls_context(db, user_id=admin_id, company_id=company_id)
    repo = UserRepository(db)

    users, total_count = await _page(repo, company_id)

    # 최근에 만든 계정부터, 같으면 user_id 순. Client 역할·이 고객사만
    assert [user.user_id for user in users] == expected
    assert total_count == len(expected)
    assert {user.company_id for user in users} == {company_id}
    assert {user.role_code for user in users} <= {
        UserRoleCode.CLIENT_MEMBER,
        UserRoleCode.CLIENT_LEAD,
    }

    # 페이지: 전체 개수는 그대로, 행은 size 만큼 잘린다
    second, second_total = await _page(
        repo, company_id, params=PageParams(page=2, size=1)
    )
    assert [user.user_id for user in second] == expected[1:2]
    assert second_total == len(expected)

    # 필터: 역할·상태별 개수를 더하면 전체와 같다
    by_role = [
        await _page(repo, company_id, role_code=role_code)
        for role_code in (UserRoleCode.CLIENT_MEMBER, UserRoleCode.CLIENT_LEAD)
    ]
    assert sum(count for _, count in by_role) == len(expected)
    for (rows, _), role_code in zip(by_role, ("CLIENT_MEMBER", "CLIENT_LEAD")):
        assert {user.role_code.value for user in rows} <= {role_code}
    by_status = [
        await _page(repo, company_id, status=status)
        for status in (UserStatus.ACTIVE, UserStatus.DISABLED)
    ]
    assert sum(count for _, count in by_status) == len(expected)


async def test_unassigned_admin_sees_no_client_users_under_app_role(db_write_session):
    db = db_write_session
    assignment = (await db.execute(_FIND_ASSIGNMENT)).one_or_none()
    if assignment is None:
        return
    _, company_id = assignment
    other_admin_id = await db.scalar(_FIND_UNASSIGNED_ADMIN, {"company_id": company_id})
    if other_admin_id is None:
        return
    await db.execute(text("SET LOCAL ROLE glowship_v4_app"))
    await set_rls_context(db, user_id=other_admin_id, company_id=company_id)

    users, total_count = await _page(UserRepository(db), company_id)

    assert (users, total_count) == ([], 0)
