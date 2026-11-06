import asyncio
import contextlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.modules.delay_analysis.application.exception.analysis_run_creation_exceptions import (
    AnalysisRunCreationError,
    RunCreationErrorCode,
)
from app.modules.delay_analysis.application.run_creation.run_code import basis_date
from app.modules.delay_analysis.application.run_creation.run_creation_service import (
    create_analysis_run,
    record_input_data_versions,
)
from app.modules.delay_analysis.infrastructure.analysis_run import AnalysisRun
from app.modules.delay_analysis.infrastructure.analysis_run_data_version import (
    AnalysisRunDataVersion,
)
from app.modules.delay_analysis.infrastructure.repository.run_creation_repository import (
    RunCreationRepository,
)

"""
    분석 실행 생성(GLOW-99) — 실제 v6.2 DB(2026 H1 + overlay) 시험
      - db_write_session 은 끝나면 롤백한다. 동시 호출 시험은 두 연결을 따로 열고 둘 다 롤백한다(DB 는 그대로).
      - overlay 실행 21건으로 운영 기준·데이터 버전 규칙이 데이터 팀 엔진 저장값과 같은지 본다.
"""

pytestmark = pytest.mark.asyncio

_EVENT_WITH_LATEST_RUN = text(
    """
    SELECT e.company_id, e.delay_event_id, e.shipment_id
      FROM delay_events e
     WHERE EXISTS (SELECT 1 FROM analysis_runs r WHERE r.delay_event_id = e.delay_event_id AND r.is_latest)
     ORDER BY e.detected_at, e.delay_event_id
     LIMIT 1
    """
)
# 그 지연의 AUTO 실행이 아직 쓰지 않은 같은 선적의 일정 스냅샷
_UNUSED_SNAPSHOTS = text(
    """
    SELECT s.shipment_schedule_snapshot_id
      FROM shipment_schedule_snapshots s
     WHERE s.shipment_id = :shipment_id
       AND NOT EXISTS (
           SELECT 1 FROM analysis_runs r
            WHERE r.delay_event_id = :delay_event_id
              AND r.trigger_shipment_schedule_snapshot_id = s.shipment_schedule_snapshot_id)
     ORDER BY s.observed_at DESC, s.shipment_schedule_snapshot_id
     LIMIT 2
    """
)
_OTHER_SHIPMENT_SNAPSHOT = text(
    """
    SELECT shipment_schedule_snapshot_id FROM shipment_schedule_snapshots
     WHERE company_id = :company_id AND shipment_id <> :shipment_id
     LIMIT 1
    """
)
_CLIENT_USER = text(
    "SELECT user_id FROM users WHERE company_id = :company_id"
    " AND role_code IN ('CLIENT_MEMBER','CLIENT_LEAD') ORDER BY user_id LIMIT 1"
)
_MAX_APPLIED_SEQ = text(
    "SELECT COALESCE(MAX(applied_seq), 0) FROM data_sync_runs WHERE company_id = :company_id"
)
# 열린 에피소드가 없는 선적에 새 에피소드(실행 없음)를 만든다. overlay 지연 8건은 모두 종료돼 있다.
# ':EP' 의 콜론은 text() 가 바인드 변수로 읽지 않게 \: 로 쓴다.
_NEW_OPEN_EPISODE = text(
    r"""
    INSERT INTO delay_events (delay_event_id, company_id, shipment_id, episode_no,
           detected_shipment_schedule_snapshot_id, stage, planned_3pl_inbound_date,
           expected_3pl_inbound_date, dedupe_key, detected_at)
    SELECT :new_id, e.company_id, e.shipment_id, m.next_no, s.shipment_schedule_snapshot_id, 'AT_SEA',
           DATE '2026-11-01', DATE '2026-11-10', e.shipment_id::text || '\:EP' || m.next_no, now()
      FROM delay_events e
      CROSS JOIN LATERAL (SELECT MAX(episode_no) + 1 AS next_no FROM delay_events x
                           WHERE x.company_id = e.company_id AND x.shipment_id = e.shipment_id) m
      CROSS JOIN LATERAL (SELECT shipment_schedule_snapshot_id FROM shipment_schedule_snapshots
                           WHERE shipment_id = e.shipment_id ORDER BY observed_at DESC LIMIT 1) s
     WHERE e.delay_event_id = :template_event_id
       AND NOT EXISTS (SELECT 1 FROM delay_events o WHERE o.shipment_id = e.shipment_id AND o.closed_at IS NULL)
    RETURNING shipment_id
    """
)


async def _target(db: AsyncSession):
    row = (await db.execute(_EVENT_WITH_LATEST_RUN)).one_or_none()
    if row is None:
        pytest.skip("현재 실행이 있는 지연이 없는 DB 라 건너뜀")
    company_id, delay_event_id, shipment_id = row
    snapshots = list(
        await db.scalars(
            _UNUSED_SNAPSHOTS,
            {"shipment_id": shipment_id, "delay_event_id": delay_event_id},
        )
    )
    if len(snapshots) < 2:
        pytest.skip("쓰지 않은 일정 스냅샷이 부족해 건너뜀")
    return company_id, delay_event_id, shipment_id, snapshots


async def _latest(db: AsyncSession, delay_event_id: uuid.UUID) -> AnalysisRun:
    return await db.scalar(
        select(AnalysisRun).where(
            AnalysisRun.delay_event_id == delay_event_id, AnalysisRun.is_latest
        )
    )


async def _run_count(db: AsyncSession, delay_event_id: uuid.UUID) -> int:
    return await db.scalar(
        select(func.count()).where(AnalysisRun.delay_event_id == delay_event_id)
    )


async def test_follow_up_auto_run_moves_is_latest_and_records_input(db_write_session):
    db = db_write_session
    company_id, delay_event_id, _, snapshots = await _target(db)
    previous = await _latest(db, delay_event_id)
    previous_status = previous.status
    max_version = await db.scalar(
        select(func.max(AnalysisRun.version_no)).where(
            AnalysisRun.delay_event_id == delay_event_id
        )
    )

    result = await create_analysis_run(
        db,
        company_id=company_id,
        delay_event_id=delay_event_id,
        trigger_reason="SCHEDULE_WORSENED",
        trigger_shipment_schedule_snapshot_id=snapshots[0],
    )

    run = result.run
    assert result.created is True
    assert result.previous_run.analysis_run_id == previous.analysis_run_id
    assert run.version_no == max_version + 1
    assert run.parent_analysis_run_id == previous.analysis_run_id
    assert run.status == "DETECTED"
    assert run.trigger_type == "AUTO"
    assert run.is_latest is True
    await db.refresh(previous)
    assert previous.is_latest is False
    assert previous.status == previous_status  # STALE 기록은 이 함수가 하지 않는다
    assert run.input_seq == await db.scalar(
        _MAX_APPLIED_SEQ, {"company_id": company_id}
    )
    assert run.calculation_contract_version == "T14-v1"
    assert run.allocation_rule_version == "D00-B-b2-v1"
    assert run.started_at.tzinfo is not None
    assert run.run_code.startswith(f"AN-{basis_date(run.started_at):%y%m%d}-")
    assert run.operating_rule_set_id == await RunCreationRepository(
        db
    ).find_operating_rule_set_id(company_id, basis_date(run.started_at))
    assert (
        await db.scalar(
            select(func.count()).where(
                AnalysisRun.delay_event_id == delay_event_id, AnalysisRun.is_latest
            )
        )
        == 1
    )


async def test_same_auto_trigger_twice_creates_one_run(db_write_session):
    db = db_write_session
    company_id, delay_event_id, _, snapshots = await _target(db)
    before = await _run_count(db, delay_event_id)
    kwargs = {
        "company_id": company_id,
        "delay_event_id": delay_event_id,
        "trigger_reason": "STAGE_CHANGED",
        "trigger_shipment_schedule_snapshot_id": snapshots[0],
    }

    first = await create_analysis_run(db, **kwargs)
    second = await create_analysis_run(db, **kwargs)

    assert first.created is True
    assert second.created is False
    assert second.previous_run is None
    assert second.run.analysis_run_id == first.run.analysis_run_id
    assert await _run_count(db, delay_event_id) == before + 1


async def test_user_reanalysis_records_requester(db_write_session):
    db = db_write_session
    company_id, delay_event_id, _, _ = await _target(db)
    user_id = await db.scalar(_CLIENT_USER, {"company_id": company_id})
    if user_id is None:
        pytest.skip("고객사 사용자가 없어 건너뜀")

    result = await create_analysis_run(
        db,
        company_id=company_id,
        delay_event_id=delay_event_id,
        trigger_reason="USER_REANALYSIS",
        requested_by_user_id=user_id,
        request_reason="수량 보완 뒤 다시 확인",
    )

    run = result.run
    assert run.trigger_type == "REANALYSIS"
    assert run.trigger_shipment_schedule_snapshot_id is None
    assert run.requested_by_user_id == user_id
    assert run.requested_at == run.started_at


async def test_given_started_at_sets_run_time_basis_and_run_code(db_write_session):
    """이슈 #50: 부르는 쪽이 넘긴 실행 시각(가상 시각)을 쓰고, 기준일·run_code·운영 기준·요청 시각이 따라온다.
    고객사 마지막 적재 수집 시작과 같은 시각은 허용한다(엔진은 시작 ≤ 실행 시각이면 입력에 넣음)."""
    db = db_write_session
    company_id, delay_event_id, _, _ = await _target(db)
    user_id = await db.scalar(_CLIENT_USER, {"company_id": company_id})
    repo = RunCreationRepository(db)
    last_input = await repo.find_last_applied_sync_started_at(company_id)

    result = await create_analysis_run(
        db,
        company_id=company_id,
        delay_event_id=delay_event_id,
        trigger_reason="USER_REANALYSIS",
        requested_by_user_id=user_id,
        started_at=last_input,
    )

    run = result.run
    assert run.started_at == last_input
    assert run.requested_at == last_input
    assert run.run_code.startswith(f"AN-{basis_date(last_input):%y%m%d}-")
    assert run.operating_rule_set_id == await repo.find_operating_rule_set_id(
        company_id, basis_date(last_input)
    )


async def test_given_started_at_before_last_input_is_rejected(db_write_session):
    """이슈 #50(rouiv 리뷰): 넘긴 시각이 마지막 적재 수집 시작보다 이르면 막는다. 아무것도 바꾸지 않는다."""
    db = db_write_session
    company_id, delay_event_id, _, snapshots = await _target(db)
    last_input = await RunCreationRepository(db).find_last_applied_sync_started_at(
        company_id
    )
    before = await _run_count(db, delay_event_id)
    previous = await _latest(db, delay_event_id)

    with pytest.raises(AnalysisRunCreationError) as raised:
        await create_analysis_run(
            db,
            company_id=company_id,
            delay_event_id=delay_event_id,
            trigger_reason="SCHEDULE_WORSENED",
            trigger_shipment_schedule_snapshot_id=snapshots[0],
            started_at=last_input - timedelta(seconds=1),
        )

    assert raised.value.code == RunCreationErrorCode.RUN_TIME_BEFORE_INPUT
    assert await _run_count(db, delay_event_id) == before
    await db.refresh(previous)
    assert previous.is_latest is True


async def test_last_applied_sync_started_at_is_whole_second_max(db_session):
    company_id, _, _, _ = await _target(db_session)
    expected = await db_session.scalar(
        text(
            "SELECT date_trunc('second', max(started_at)) FROM data_sync_runs"
            " WHERE company_id = :c AND applied_seq IS NOT NULL"
        ),
        {"c": company_id},
    )

    got = await RunCreationRepository(db_session).find_last_applied_sync_started_at(
        company_id
    )

    assert got == expected
    assert got.microsecond == 0
    assert (
        await RunCreationRepository(db_session).find_last_applied_sync_started_at(
            uuid.UUID("00000000-0000-7000-8000-000000000000")
        )
        is None
    )


async def test_runs_of_one_day_get_consecutive_run_codes(db_write_session):
    db = db_write_session
    company_id, delay_event_id, _, snapshots = await _target(db)

    first = await create_analysis_run(
        db,
        company_id=company_id,
        delay_event_id=delay_event_id,
        trigger_reason="SCHEDULE_WORSENED",
        trigger_shipment_schedule_snapshot_id=snapshots[0],
    )
    second = await create_analysis_run(
        db,
        company_id=company_id,
        delay_event_id=delay_event_id,
        trigger_reason="SCHEDULE_IMPROVED",
        trigger_shipment_schedule_snapshot_id=snapshots[1],
    )

    a, b = first.run.run_code, second.run.run_code
    assert a[:10] == b[:10]
    assert int(b[-3:]) == int(a[-3:]) + 1
    assert second.run.parent_analysis_run_id == first.run.analysis_run_id


async def test_first_run_of_new_episode_is_version_one(db_write_session):
    db = db_write_session
    template_company, template_event, _, _ = await _target(db)
    new_event_id = uuid.uuid7()
    inserted = await db.scalar(
        _NEW_OPEN_EPISODE, {"new_id": new_event_id, "template_event_id": template_event}
    )
    if inserted is None:
        pytest.skip("열린 에피소드가 이미 있는 선적이라 새 에피소드를 만들 수 없음")
    snapshot_id = (
        await db.execute(
            text(
                "SELECT detected_shipment_schedule_snapshot_id FROM delay_events WHERE delay_event_id = :id"
            ),
            {"id": new_event_id},
        )
    ).scalar_one()

    with pytest.raises(AnalysisRunCreationError) as error:
        await create_analysis_run(
            db,
            company_id=template_company,
            delay_event_id=new_event_id,
            trigger_reason="SCHEDULE_WORSENED",
            trigger_shipment_schedule_snapshot_id=snapshot_id,
        )
    assert error.value.code == RunCreationErrorCode.EPISODE_START_MISMATCH

    result = await create_analysis_run(
        db,
        company_id=template_company,
        delay_event_id=new_event_id,
        trigger_reason="EPISODE_STARTED",
        trigger_shipment_schedule_snapshot_id=snapshot_id,
    )
    assert result.run.version_no == 1
    assert result.run.parent_analysis_run_id is None
    assert result.previous_run is None


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"trigger_reason": "SOMETHING_ELSE"}, RunCreationErrorCode.INVALID_TRIGGER),
        (
            {"trigger_reason": "SCHEDULE_WORSENED"},
            RunCreationErrorCode.INVALID_TRIGGER,
        ),
        ({"trigger_reason": "USER_REANALYSIS"}, RunCreationErrorCode.INVALID_TRIGGER),
        (
            {"trigger_reason": "EPISODE_STARTED", "use_snapshot": True},
            RunCreationErrorCode.EPISODE_START_MISMATCH,
        ),
        (
            {
                "trigger_reason": "SCHEDULE_WORSENED",
                "use_snapshot": True,
                "request_reason": "x",
            },
            RunCreationErrorCode.INVALID_TRIGGER,
        ),
    ],
)
async def test_invalid_requests_change_nothing(db_write_session, kwargs, code):
    db = db_write_session
    company_id, delay_event_id, _, snapshots = await _target(db)
    previous = await _latest(db, delay_event_id)
    before = await _run_count(db, delay_event_id)
    kwargs = dict(kwargs)
    snapshot = snapshots[0] if kwargs.pop("use_snapshot", False) else None

    with pytest.raises(AnalysisRunCreationError) as error:
        await create_analysis_run(
            db,
            company_id=company_id,
            delay_event_id=delay_event_id,
            trigger_shipment_schedule_snapshot_id=snapshot,
            **kwargs,
        )

    assert error.value.code == code
    assert await _run_count(db, delay_event_id) == before
    assert (
        await _latest(db, delay_event_id)
    ).analysis_run_id == previous.analysis_run_id


async def test_snapshot_of_another_shipment_is_rejected(db_write_session):
    db = db_write_session
    company_id, delay_event_id, shipment_id, _ = await _target(db)
    other = await db.scalar(
        _OTHER_SHIPMENT_SNAPSHOT, {"company_id": company_id, "shipment_id": shipment_id}
    )

    with pytest.raises(AnalysisRunCreationError) as error:
        await create_analysis_run(
            db,
            company_id=company_id,
            delay_event_id=delay_event_id,
            trigger_reason="SCHEDULE_WORSENED",
            trigger_shipment_schedule_snapshot_id=other,
        )
    assert error.value.code == RunCreationErrorCode.SNAPSHOT_NOT_OF_SHIPMENT


async def test_unknown_delay_event_is_rejected(db_write_session):
    db = db_write_session
    company_id, _, _, snapshots = await _target(db)

    with pytest.raises(AnalysisRunCreationError) as error:
        await create_analysis_run(
            db,
            company_id=company_id,
            delay_event_id=uuid.uuid7(),
            trigger_reason="SCHEDULE_WORSENED",
            trigger_shipment_schedule_snapshot_id=snapshots[0],
        )
    assert error.value.code == RunCreationErrorCode.DELAY_EVENT_NOT_FOUND


async def test_record_input_data_versions_is_idempotent(db_write_session):
    db = db_write_session
    company_id, delay_event_id, _, snapshots = await _target(db)
    run = (
        await create_analysis_run(
            db,
            company_id=company_id,
            delay_event_id=delay_event_id,
            trigger_reason="SCHEDULE_WORSENED",
            trigger_shipment_schedule_snapshot_id=snapshots[0],
        )
    ).run

    first = await record_input_data_versions(db, run)
    second = await record_input_data_versions(db, run)

    rows = await db.scalar(
        select(func.count()).where(
            AnalysisRunDataVersion.analysis_run_id == run.analysis_run_id
        )
    )
    assert first == second
    assert rows == len(first) == 5  # PRODUCT·WAREHOUSE·ORDER·SHIPMENT·INVENTORY


async def test_rules_match_overlay_runs(db_session):
    """overlay 실행 21건: 운영 기준은 21건, 데이터 버전은 검증을 통과한 20건이 엔진 저장값과 같다"""
    db = db_session
    repo = RunCreationRepository(db)
    runs = list(await db.scalars(select(AnalysisRun)))
    if not runs:
        pytest.skip("분석 실행이 없는 DB 라 건너뜀")
    checked = 0
    for run in runs:
        assert run.operating_rule_set_id == await repo.find_operating_rule_set_id(
            run.company_id, basis_date(run.started_at)
        ), run.analysis_run_id
        recorded = await repo.find_recorded_sync_run_ids(run.analysis_run_id)
        if run.completed_status == "ON_HOLD" or run.status == "ON_HOLD":
            assert recorded == set()  # 입력 검증에 실패한 실행은 데이터 버전이 없다
            continue
        expected = await repo.find_domain_latest_sync_run_ids(
            run.company_id, run.input_seq
        )
        assert set(expected) == recorded, run.analysis_run_id
        checked += 1
    assert checked >= 1


async def _open_session():
    engine = create_async_engine(
        get_settings().database_url, poolclass=NullPool, connect_args={"timeout": 2}
    )
    try:
        connection = await engine.connect()
    except (OSError, SQLAlchemyError) as error:
        await engine.dispose()
        pytest.skip(f"DB 에 접속할 수 없어 건너뜀: {type(error).__name__}")
    transaction = await connection.begin()
    return engine, connection, transaction, AsyncSession(bind=connection)


async def test_concurrent_calls_for_same_company_wait_on_company_lock(db_session):
    """같은 고객사 두 호출: 뒤 호출은 앞 트랜잭션이 끝날 때까지 L1(advisory lock)에서 기다린다"""
    company_id, delay_event_id, _, snapshots = await _target(db_session)
    a = await _open_session()
    b = await _open_session()
    task = None
    try:
        _, _, a_tx, a_db = a
        _, b_conn, _, b_db = b
        kwargs = {
            "company_id": company_id,
            "delay_event_id": delay_event_id,
            "trigger_reason": "SCHEDULE_WORSENED",
            "trigger_shipment_schedule_snapshot_id": snapshots[0],
        }
        first = await create_analysis_run(a_db, **kwargs)
        assert first.created is True
        b_pid = (await b_conn.execute(text("SELECT pg_backend_pid()"))).scalar_one()

        task = asyncio.create_task(create_analysis_run(b_db, **kwargs))
        wait_event = None
        for _ in range(50):
            await asyncio.sleep(0.05)
            wait_event = await a_db.scalar(
                text("SELECT wait_event FROM pg_stat_activity WHERE pid = :pid"),
                {"pid": b_pid},
            )
            if wait_event == "advisory":
                break
        assert (
            wait_event == "advisory"
        )  # 행·고유 인덱스가 아니라 고객사 잠금에서 기다린다
        assert not task.done()

        await a_tx.rollback()  # 앞 호출을 되돌리면 뒤 호출이 진행해 새로 만든다
        second = await asyncio.wait_for(task, timeout=10)
        assert second.created is True
        assert second.run.started_at > datetime(2000, 1, 1, tzinfo=UTC)
    finally:
        # 실패해도 정리되도록: 앞 트랜잭션을 먼저 되돌려 뒤 호출을 풀고, 뒤 호출이 끝난 뒤 연결을 닫는다
        if a[2].is_active:
            await a[2].rollback()
        if task is not None and not task.done():
            with contextlib.suppress(Exception):
                await asyncio.wait_for(task, timeout=10)
        for engine, connection, transaction, session in (b, a):
            await session.close()
            if transaction.is_active:
                await transaction.rollback()
            await connection.close()
            await engine.dispose()
