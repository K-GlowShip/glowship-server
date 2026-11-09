from sqlalchemy import text

"""
    로그인 조회 함수 app_auth_find_login_user (마이그레이션 0008, 이슈 #41) — 실제 DB, 끝나면 롤백
      - 앱 역할(glowship_v4_app)로 바꾸면 컨텍스트 없이 users 를 직접 읽을 때 0행이다(users_select_tenant_or_self).
        함수로는 그 이메일 한 명이 보인다. 없는 이메일은 0행.
      - 실행 권한은 앱 역할에만 있다(워커 역할·PUBLIC 없음).
      - 로컬 개발 DB 는 superuser 라 SET LOCAL ROLE 로 역할을 바꿔 정책을 적용한다(트랜잭션이 끝나면 되돌아감).
      - DB 가 없거나 사용자가 없으면 건너뜁니다(tests/conftest.py 픽스처).
"""

APP_ROLE = "glowship_v4_app"
WORKER_ROLE = "glowship_v4_worker"


async def _any_user(db):
    return (
        await db.execute(
            text(
                "SELECT user_id, company_id, role_code, password_hash, status, email"
                " FROM users ORDER BY email LIMIT 1"
            )
        )
    ).one_or_none()


async def test_app_role_finds_login_user_only_through_function(db_write_session):
    db = db_write_session
    user = await _any_user(db)
    if user is None:
        return  # 사용자가 없는 DB
    await db.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))

    direct = await db.scalar(
        text("SELECT count(*) FROM users WHERE email = :e"), {"e": user.email}
    )
    found = (
        await db.execute(
            text("SELECT * FROM app_auth_find_login_user(:e)"), {"e": user.email}
        )
    ).all()
    missing = await db.scalar(
        text("SELECT count(*) FROM app_auth_find_login_user(:e)"),
        {"e": "nobody@example.invalid"},
    )

    assert direct == 0  # 수정 전 로그인 조회: 앱 역할에서는 보이지 않음
    assert [tuple(r) for r in found] == [
        (
            user.user_id,
            user.company_id,
            str(user.role_code),
            user.password_hash,
            str(user.status),
        )
    ]
    assert missing == 0


async def test_only_app_role_can_execute(db_session):
    rows = dict(
        (
            await db_session.execute(
                text(
                    "SELECT r, has_function_privilege(r, 'app_auth_find_login_user(text)', 'EXECUTE')"
                    " FROM unnest(ARRAY[:app, :worker]) AS r"
                ),
                {"app": APP_ROLE, "worker": WORKER_ROLE},
            )
        ).all()
    )
    owner = await db_session.scalar(
        text(
            "SELECT pg_get_userbyid(proowner) FROM pg_proc"
            " WHERE proname = 'app_auth_find_login_user'"
        )
    )

    assert rows == {APP_ROLE: True, WORKER_ROLE: False}
    assert owner == "glowship_v4_owner"
