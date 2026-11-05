import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

import app.core.model_registry  # noqa: F401  (모든 모델 등록)
from app.core.orm import Base

"""
    0007: 워커 역할에 delay_events.closed_at·close_reason 만 UPDATE (GLOW-117)
      - 리비전 SQL 이 두 열만 다루는지, 역할 이름이 0001·DDL 과 같은지 (DB 없이)
      - 0007 을 적용한 DB 에서 워커가 UPDATE 할 수 있는 열이 정확히 두 열인지 (권한 목록)
      - 워커 역할로 바꿔 두 열 UPDATE 는 성공하고 다른 열은 권한 오류인지, 다른 고객사 행은 RLS 로 0행인지 (실제 실행)
      - downgrade SQL 이 권한을 거두는지
    실제 DB 시험은 모두 롤백한다. DB 가 0007 이 아니면 건너뛴다.
"""

VERSIONS = Path(__file__).resolve().parents[3] / "migrations" / "versions"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, VERSIONS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


M0007 = _load("0007_grant_worker_delay_event_close")
M0001 = _load("0001_create_glowship_v5_schema")
WORKER = M0007.WORKER_ROLE
CLOSE = set(M0007.CLOSE_COLUMNS)
INSUFFICIENT_PRIVILEGE = "42501"

_UPDATABLE_BY_WORKER = text(
    "SELECT column_name FROM information_schema.columns"
    " WHERE table_schema = 'public' AND table_name = 'delay_events'"
    " AND has_column_privilege(:role, 'delay_events', column_name, 'UPDATE')"
)


async def _updatable_columns(session) -> set[str]:
    rows = await session.execute(_UPDATABLE_BY_WORKER, {"role": WORKER})
    return set(rows.scalars())


async def _require_0007(session) -> None:
    version = await session.scalar(text("SELECT version_num FROM alembic_version"))
    if version is None or not version.isdigit() or int(version) < 7:
        pytest.skip(f"마이그레이션 0007 이 적용되지 않은 DB 라 건너뜀 (지금 {version})")


def test_revision_touches_only_close_columns():
    assert M0007.revision == "0007" and M0007.down_revision == "0006"
    assert M0007.CLOSE_COLUMNS == ("closed_at", "close_reason")
    assert WORKER == M0001.WORKER_ROLE == "glowship_v4_worker"
    assert (
        M0007.GRANT_SQL
        == "GRANT UPDATE (closed_at, close_reason) ON delay_events TO glowship_v4_worker"
    )
    assert (
        M0007.REVOKE_SQL
        == "REVOKE UPDATE (closed_at, close_reason) ON delay_events FROM glowship_v4_worker"
    )
    assert CLOSE <= set(Base.metadata.tables["delay_events"].columns.keys())


async def test_worker_can_update_only_close_columns(db_session):
    await _require_0007(db_session)
    assert await _updatable_columns(db_session) == CLOSE
    privileges = (
        await db_session.execute(
            text(
                "SELECT has_table_privilege(:r, 'delay_events', 'UPDATE'),"
                " has_table_privilege(:r, 'delay_events', 'DELETE'),"
                " has_table_privilege(:r, 'delay_events', 'SELECT'),"
                " has_table_privilege(:r, 'delay_events', 'INSERT')"
            ),
            {"r": WORKER},
        )
    ).one()
    assert tuple(privileges) == (
        False,
        False,
        True,
        True,
    )  # 표 전체 UPDATE·DELETE 없음, 조회·추가는 DDL 그대로


async def test_privilege_check_catches_wider_grant(db_write_session):
    """음성: 표 전체 UPDATE 를 주면 위 검사가 두 열 밖의 열을 잡는다 (롤백)"""
    await _require_0007(db_write_session)
    await db_write_session.execute(text(f"GRANT UPDATE ON delay_events TO {WORKER}"))
    wider = await _updatable_columns(db_write_session)
    assert wider > CLOSE and "episode_no" in wider


async def test_worker_closes_episode_but_cannot_change_start_values(db_write_session):
    await _require_0007(db_write_session)
    row = (
        await db_write_session.execute(
            text(
                "SELECT de.delay_event_id::text, de.company_id::text FROM delay_events de"
                " JOIN companies c ON c.company_id = de.company_id WHERE c.status = 'ACTIVE'"
                " ORDER BY de.delay_event_id LIMIT 1"
            )
        )
    ).one_or_none()
    if row is None:
        pytest.skip("활성 고객사의 지연 에피소드가 없는 DB 라 건너뜀")
    event_id, company_id = row
    params = {"id": event_id}

    await db_write_session.execute(
        text("SELECT set_config('app.company_id', :c, true)"), {"c": company_id}
    )
    await db_write_session.execute(text(f"SET LOCAL ROLE {WORKER}"))
    assert await db_write_session.scalar(text("SELECT current_user")) == WORKER

    # 종료 열: 같은 값을 다시 써서 권한·RLS 만 확인한다 (행 값은 바뀌지 않음)
    closed = await db_write_session.execute(
        text(
            "UPDATE delay_events SET closed_at = closed_at, close_reason = close_reason"
            " WHERE delay_event_id = CAST(:id AS uuid)"
        ),
        params,
    )
    assert closed.rowcount == 1

    # 시작 값: 권한 오류. 메시지는 DB 언어 설정(lc_messages)에 따라 달라서 SQLSTATE 42501(insufficient_privilege)로 본다
    for column in ("episode_no", "stage", "expected_3pl_inbound_date", "shipment_id"):
        with pytest.raises(DBAPIError) as denied:
            async with db_write_session.begin_nested():
                await db_write_session.execute(
                    text(
                        f"UPDATE delay_events SET {column} = {column}"
                        " WHERE delay_event_id = CAST(:id AS uuid)"
                    ),
                    params,
                )
        assert denied.value.orig.sqlstate == INSUFFICIENT_PRIVILEGE, column

    # 다른 고객사 범위: 권한은 있어도 RLS 로 0행
    await db_write_session.execute(
        text(
            "SELECT set_config('app.company_id', '00000000-0000-0000-0000-000000000000', true)"
        )
    )
    other = await db_write_session.execute(
        text(
            "UPDATE delay_events SET close_reason = close_reason"
            " WHERE delay_event_id = CAST(:id AS uuid)"
        ),
        params,
    )
    assert other.rowcount == 0


async def test_downgrade_sql_revokes_close_columns(db_write_session):
    await _require_0007(db_write_session)
    await db_write_session.execute(text(M0007.REVOKE_SQL))
    assert await _updatable_columns(db_write_session) == set()
    await db_write_session.execute(text(M0007.GRANT_SQL))
    assert await _updatable_columns(db_write_session) == CLOSE
