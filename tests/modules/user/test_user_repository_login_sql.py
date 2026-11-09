from sqlalchemy import text

from app.modules.user.infrastructure.repository.user_repository import (
    LoginUser,
    UserRepository,
)
from app.modules.user.infrastructure.user_enums import UserRoleCode, UserStatus

"""
    로그인 조회 리포지토리 (이슈 #41) — 실제 DB, 끝나면 롤백
      - 앱 DB 역할(glowship_v4_app)로 바꾸고 컨텍스트 없이 조회한다(로그인 순간과 같은 상태).
      - find_login_user(DB 함수 0008)는 그 사용자를 찾고, find_by_email(select User)은 0행이라 None 이다.
      - 로컬 개발 DB 는 superuser 라 SET LOCAL ROLE 로 정책을 적용한다. 사용자가 없는 DB 면 확인하지 않는다.
"""


async def test_login_lookup_works_under_app_role_without_context(db_write_session):
    db = db_write_session
    user = (
        await db.execute(
            text(
                "SELECT user_id, company_id, role_code, password_hash, status, email"
                " FROM users WHERE role_code = 'CLIENT_MEMBER' ORDER BY email LIMIT 1"
            )
        )
    ).one_or_none()
    if user is None:
        return
    await db.execute(text("SET LOCAL ROLE glowship_v4_app"))
    repo = UserRepository(db)

    found = await repo.find_login_user(user.email)
    by_select = await repo.find_by_email(user.email)

    assert found == LoginUser(
        user_id=user.user_id,
        company_id=user.company_id,
        role_code=UserRoleCode.CLIENT_MEMBER,
        password_hash=user.password_hash,
        status=UserStatus(user.status),
    )
    assert by_select is None  # 수정 전 로그인 조회 경로
    assert await repo.find_login_user("nobody@example.invalid") is None
