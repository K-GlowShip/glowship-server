from sqlalchemy import text

"""
    끝나지 않은 분석 실행 조회 함수 app_worker_find_unfinished_runs (마이그레이션 0009, GLOW-128) — 실제 DB, 끝나면 롤백
      - 서버 시작 정리는 모든 고객사의 남은 실행을 봐야 한다. 워커 역할(glowship_v4_worker)로 바꾸면 고객사 문맥 없이
        analysis_runs 를 직접 읽을 때 0행이고(고객사 행 보안), 함수로는 끝나지 않은 실행만 보인다.
      - 실행 권한은 워커 역할에만 있다(앱 역할·PUBLIC 없음).
      - 로컬 개발 DB 는 superuser 라 SET LOCAL ROLE 로 역할을 바꿔 정책을 적용한다(트랜잭션이 끝나면 되돌아감).
      - 확정 DB 실행은 모두 끝난 상태라, 보류(ON_HOLD) 실행 하나를 같은 트랜잭션 안에서 DETECTED 로 되돌려 쓴다.
        DB 가 없거나 보류 실행이 없으면 건너뜁니다(tests/conftest.py 픽스처).
"""

APP_ROLE = "glowship_v4_app"
WORKER_ROLE = "glowship_v4_worker"
FUNCTION = "app_worker_find_unfinished_runs()"


async def _make_one_detected(db):
    """보류 실행 하나를 DETECTED 로 되돌린다(실패 요약 5열·완료 상태를 비움, DDL ck10·ck11·ck19 를 지킴)."""
    return (
        await db.execute(
            text(
                "UPDATE analysis_runs SET status = 'DETECTED', completed_status = NULL,"
                " failed_step = NULL, failure_code = NULL, attempt_count = NULL,"
                " retry_exhausted = NULL, failure_message = NULL, finished_at = NULL"
                " WHERE analysis_run_id = ("
                "   SELECT analysis_run_id FROM analysis_runs WHERE status = 'ON_HOLD'"
                "   ORDER BY analysis_run_id LIMIT 1)"
                " RETURNING company_id, analysis_run_id"
            )
        )
    ).one_or_none()


async def test_worker_finds_unfinished_runs_only_through_function(db_write_session):
    db = db_write_session
    detected = await _make_one_detected(db)
    if detected is None:
        return  # 보류 실행이 없는 DB
    expected = (
        await db.execute(
            text(
                "SELECT r.company_id, r.analysis_run_id, r.status FROM analysis_runs r"
                " JOIN companies c ON c.company_id = r.company_id AND c.status = 'ACTIVE'"
                " WHERE r.status IN ('DETECTED','VALIDATING','ANALYZING_IMPACT',"
                "                    'ANALYZING_OPTIONS','VALIDATING_OUTPUT')"
                " ORDER BY r.company_id, r.started_at, r.analysis_run_id"
            )
        )
    ).all()
    await db.execute(text(f"SET LOCAL ROLE {WORKER_ROLE}"))

    direct = await db.scalar(text("SELECT count(*) FROM analysis_runs"))
    found = (await db.execute(text(f"SELECT * FROM {FUNCTION}"))).all()

    assert direct == 0  # 고객사 문맥 없는 워커 역할: 행이 하나도 보이지 않음
    assert [tuple(r) for r in found] == [tuple(r) for r in expected]
    assert (detected.company_id, detected.analysis_run_id, "DETECTED") in [
        tuple(r) for r in found
    ]
    # 끝난 실행은 돌려주지 않는다
    assert all(r.status != "ON_HOLD" for r in found)


async def test_only_worker_role_can_execute(db_session):
    rows = dict(
        (
            await db_session.execute(
                text(
                    "SELECT r, has_function_privilege(r, :fn, 'EXECUTE')"
                    " FROM unnest(ARRAY[:app, :worker]) AS r"
                ),
                {"fn": FUNCTION, "app": APP_ROLE, "worker": WORKER_ROLE},
            )
        ).all()
    )
    public = await db_session.scalar(
        text(
            "SELECT count(*) FROM pg_proc p, aclexplode(p.proacl) a"
            " WHERE p.proname = 'app_worker_find_unfinished_runs' AND a.grantee = 0"
        )
    )
    owner = await db_session.scalar(
        text(
            "SELECT pg_get_userbyid(proowner) FROM pg_proc"
            " WHERE proname = 'app_worker_find_unfinished_runs'"
        )
    )

    assert rows == {APP_ROLE: False, WORKER_ROLE: True}
    assert public == 0
    assert owner == "glowship_v4_owner"
