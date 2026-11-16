import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.core.rls_context import set_rls_context
from app.modules.decision.application.exception.decision_exceptions import (
    DecisionErrorCode,
    DecisionException,
)
from app.modules.delay_analysis.application.run_start import outcome_save
from tests.modules.decision.test_final_decision_service_sql import (
    NOW as DECISION_NOW,
)
from tests.modules.decision.test_final_decision_service_sql import (
    _decide,
    _ready_run,
)

"""
    엔진이 쓴 운영 기준을 실행 행에 맞추는 저장 (GLOW-160) — 실제 DB, 끝나면 롤백
      - 선택 대기 실행 하나와 같은 고객사의 새 운영 기준 버전(선택일에 적용 중)을 만들어, 행의 버전과 엔진이 쓴 버전이
        어긋난 두 경우를 만든다.
          · 행은 이전 버전, 엔진은 새 버전(실행 생성 뒤·분석 시작 전에 새 버전 저장): 맞춘 뒤에는 최종 선택이 된다
          · 행은 새 버전, 엔진은 이전 버전(기준일 D 가 실행 날짜보다 앞섬): 맞춘 뒤에는 최종 선택이 OPERATING_RULE_CHANGED
      - 분석 상세가 읽는 버전 번호(행의 운영 기준 → version_no)도 엔진 쪽으로 바뀌는지, 워커 역할로 쓸 수 있는지 본다.
      - 맞춘 버전이 저장하는 날(끝난 시각 날짜)의 활성 버전이 아니면 선택 대기 실행을 STALE(OPERATING_RULE_CHANGED)로
        남기는지 본다(PR #160 리뷰). 결과 행은 그대로이고, 활성 버전과 같음·미래 버전·닫힌 지연·현재 실행이 아님은 그대로 둔다.
"""

_RULE_OF_RUN = text(
    "SELECT r.operating_rule_set_id, s.version_no FROM analysis_runs r"
    " JOIN operating_rule_sets s ON s.company_id = r.company_id"
    "  AND s.operating_rule_set_id = r.operating_rule_set_id"
    " WHERE r.analysis_run_id = :r"
)


async def _new_effective_version(db, run) -> SimpleNamespace:
    """고객사의 가장 높은 버전을 복사한 새 버전(선택일부터 적용). 리드타임도 그대로 옮긴다"""
    new_id = uuid.uuid7()
    version_no = await db.scalar(
        text(
            """
            INSERT INTO operating_rule_sets
                   (operating_rule_set_id, company_id, version_no, user_id, effective_from,
                    safety_stock_days, sales_basis_days, allow_partial_fulfillment, created_at)
            SELECT :new, s.company_id, s.version_no + 1, s.user_id, :day,
                   s.safety_stock_days, s.sales_basis_days, s.allow_partial_fulfillment, :now
              FROM operating_rule_sets s
             WHERE s.company_id = :c
             ORDER BY s.version_no DESC LIMIT 1
            RETURNING version_no
            """
        ),
        {
            "new": new_id,
            "c": run.company_id,
            "day": DECISION_NOW.date(),
            "now": DECISION_NOW,
        },
    )
    if version_no is None:
        pytest.skip("운영 기준이 없는 고객사")
    await db.execute(
        text(
            """
            INSERT INTO operating_lead_times
                   (operating_lead_time_id, company_id, operating_rule_set_id, lead_time_type,
                    warehouse_id, to_region_code, days, day_basis)
            SELECT gen_random_uuid(), lt.company_id, :new, lt.lead_time_type,
                   lt.warehouse_id, lt.to_region_code, lt.days, lt.day_basis
              FROM operating_lead_times lt
              JOIN operating_rule_sets s
                ON s.company_id = lt.company_id
               AND s.operating_rule_set_id = lt.operating_rule_set_id
             WHERE lt.company_id = :c AND s.version_no = :previous
            """
        ),
        {"new": new_id, "c": run.company_id, "previous": version_no - 1},
    )
    return SimpleNamespace(operating_rule_set_id=str(new_id), version_no=version_no)


async def _rule_of(db, run):
    return (await db.execute(_RULE_OF_RUN, {"r": run.analysis_run_id})).one()


def _started(run, rule_set_id) -> SimpleNamespace:
    """저장이 읽는 실행 값(StartedRun 의 칸). rule_set_id = 실행을 만들 때 행에 적은 운영 기준"""
    return SimpleNamespace(
        analysis_run_id=run.analysis_run_id,
        company_id=run.company_id,
        operating_rule_set_id=rule_set_id,
    )


async def test_run_recorded_on_old_version_but_computed_on_new_can_be_decided(
    db_write_session,
):
    db = db_write_session
    run = await _ready_run(db)
    old = await _rule_of(db, run)
    new = await _new_effective_version(db, run)
    # 맞추기 전: 행이 이전 버전이라 새 기준으로 계산했어도 최종 선택이 막힌다
    with pytest.raises(DecisionException) as raised:
        await _decide(db, run)
    assert raised.value.error_code is DecisionErrorCode.OPERATING_RULE_CHANGED

    await outcome_save._record_engine_operating_rule(
        db, _started(run, old.operating_rule_set_id), new
    )

    aligned = await _rule_of(db, run)
    assert str(aligned.operating_rule_set_id) == new.operating_rule_set_id
    assert aligned.version_no == new.version_no == old.version_no + 1
    assert (await _decide(db, run, key="key-2")).status == "FINALIZED"


async def test_run_recorded_on_new_version_but_computed_on_old_is_blocked(
    db_write_session,
):
    db = db_write_session
    run = await _ready_run(db)
    old = await _rule_of(db, run)
    new = await _new_effective_version(db, run)
    # 실행을 만들 때 실행 날짜로 새 버전을 골랐지만 엔진은 기준일 D(더 이른 날)로 이전 버전을 쓴 경우
    await db.execute(
        text(
            "UPDATE analysis_runs SET operating_rule_set_id = :s WHERE analysis_run_id = :r"
        ),
        {"s": new.operating_rule_set_id, "r": run.analysis_run_id},
    )
    engine_used = SimpleNamespace(
        operating_rule_set_id=str(old.operating_rule_set_id), version_no=old.version_no
    )

    await outcome_save._record_engine_operating_rule(
        db, _started(run, uuid.UUID(new.operating_rule_set_id)), engine_used
    )

    aligned = await _rule_of(db, run)
    assert (aligned.operating_rule_set_id, aligned.version_no) == tuple(old)
    with pytest.raises(DecisionException) as raised:
        await _decide(db, run)
    assert raised.value.error_code is DecisionErrorCode.OPERATING_RULE_CHANGED


async def test_same_version_is_not_written(db_write_session):
    db = db_write_session
    run = await _ready_run(db)
    old = await _rule_of(db, run)
    xmin_sql = text(
        "SELECT xmin::text, ctid::text FROM analysis_runs WHERE analysis_run_id = :r"
    )
    before = (await db.execute(xmin_sql, {"r": run.analysis_run_id})).one()

    await outcome_save._record_engine_operating_rule(
        db,
        _started(run, old.operating_rule_set_id),
        SimpleNamespace(
            operating_rule_set_id=str(old.operating_rule_set_id),
            version_no=old.version_no,
        ),
    )

    assert (await db.execute(xmin_sql, {"r": run.analysis_run_id})).one() == before


async def test_worker_role_can_align_run_row(db_write_session):
    """분석 실행기는 워커 역할(행 보안 적용)로 저장한다. 그 역할로 운영 기준 칸을 고칠 수 있어야 한다(0001 표 단위 권한)."""
    db = db_write_session
    run = await _ready_run(db)
    old = await _rule_of(db, run)
    new = await _new_effective_version(db, run)
    await set_rls_context(db, user_id=None, company_id=run.company_id)
    await db.execute(text("SET LOCAL ROLE glowship_v4_worker"))

    await outcome_save._record_engine_operating_rule(
        db, _started(run, old.operating_rule_set_id), new
    )

    assert (
        str((await _rule_of(db, run)).operating_rule_set_id)
        == new.operating_rule_set_id
    )


# --- 맞춘 버전이 저장하는 날의 활성 버전이 아니면 STALE (PR #160 리뷰) -----------------------

_STALE_OF_RUN = text(
    "SELECT status, completed_status, stale_primary_reason, stale_reason_codes,"
    "       stale_detected_seq, stale_detected_at, finished_at,"
    "       (SELECT count(*) FROM response_options o"
    "         WHERE o.analysis_run_id = r.analysis_run_id) AS options"
    "  FROM analysis_runs r WHERE r.analysis_run_id = :r"
)


async def _finish_on_decision_day(db, run) -> None:
    """실행이 끝난 시각을 선택일로 옮긴다(저장하는 날 = 새 버전이 적용되는 날)."""
    await db.execute(
        text("UPDATE analysis_runs SET finished_at = :t WHERE analysis_run_id = :r"),
        {"t": DECISION_NOW, "r": run.analysis_run_id},
    )


async def _stale_of(db, run):
    return (await db.execute(_STALE_OF_RUN, {"r": run.analysis_run_id})).one()


async def _max_applied_seq(db, run) -> int:
    return await db.scalar(
        text(
            "SELECT max(applied_seq) FROM data_sync_runs"
            " WHERE company_id = :c AND applied_seq IS NOT NULL"
        ),
        {"c": run.company_id},
    )


async def test_run_computed_on_version_no_longer_active_is_saved_stale(
    db_write_session,
):
    """엔진이 이전 버전으로 계산을 마친 뒤·저장 전에 "오늘부터 적용" 새 버전이 저장된 경우: 행은 맞출 것이 없고 STALE."""
    db = db_write_session
    run = await _ready_run(db)
    old = await _rule_of(db, run)
    await _finish_on_decision_day(db, run)
    await _new_effective_version(db, run)
    before = await _stale_of(db, run)

    assert await outcome_save._stale_if_operating_rule_not_active(
        db, _started(run, old.operating_rule_set_id)
    )

    after = await _stale_of(db, run)
    assert (after.status, after.completed_status) == ("STALE", "READY_FOR_REVIEW")
    assert after.stale_primary_reason == "OPERATING_RULE_CHANGED"
    assert after.stale_reason_codes == ["OPERATING_RULE_CHANGED"]
    assert after.stale_detected_seq == await _max_applied_seq(db, run)
    assert after.stale_detected_at == after.finished_at == DECISION_NOW
    assert after.options == before.options > 0  # 저장한 결과는 그대로
    assert (await _rule_of(db, run)) == old  # 행의 운영 기준은 계산에 쓴 버전 그대로
    with pytest.raises(DecisionException) as raised:
        await _decide(db, run)
    assert raised.value.error_code is DecisionErrorCode.ANALYSIS_STALE


async def test_run_aligned_to_old_version_is_saved_stale(db_write_session):
    """기준일 D 가 당겨져 이전 버전으로 계산한 경우: 행을 이전 버전으로 맞춘 뒤, 새 버전이 적용 중이라 STALE."""
    db = db_write_session
    run = await _ready_run(db)
    old = await _rule_of(db, run)
    await _finish_on_decision_day(db, run)
    new = await _new_effective_version(db, run)
    await db.execute(
        text(
            "UPDATE analysis_runs SET operating_rule_set_id = :s WHERE analysis_run_id = :r"
        ),
        {"s": new.operating_rule_set_id, "r": run.analysis_run_id},
    )
    started = _started(run, uuid.UUID(new.operating_rule_set_id))
    await outcome_save._record_engine_operating_rule(
        db,
        started,
        SimpleNamespace(
            operating_rule_set_id=str(old.operating_rule_set_id),
            version_no=old.version_no,
        ),
    )

    assert await outcome_save._stale_if_operating_rule_not_active(db, started)

    after = await _stale_of(db, run)
    assert (after.status, after.stale_primary_reason) == (
        "STALE",
        "OPERATING_RULE_CHANGED",
    )
    assert (await _rule_of(db, run)) == old


@pytest.mark.parametrize(
    "case", ["active", "aligned_to_new", "future", "closed", "not_latest"]
)
async def test_run_is_left_ready_when_rule_check_does_not_apply(db_write_session, case):
    db = db_write_session
    run = await _ready_run(db)
    old = await _rule_of(db, run)
    await _finish_on_decision_day(db, run)
    started = _started(run, old.operating_rule_set_id)
    if case != "active":
        new = await _new_effective_version(db, run)
    if case == "aligned_to_new":
        # 새 버전으로 계산한 실행(행을 새 버전으로 맞춤)은 활성 버전과 같다
        await outcome_save._record_engine_operating_rule(db, started, new)
    if case == "future":
        await db.execute(
            text(
                "UPDATE operating_rule_sets SET effective_from = :d"
                " WHERE operating_rule_set_id = :s"
            ),
            {
                "d": DECISION_NOW.date() + timedelta(days=1),
                "s": new.operating_rule_set_id,
            },
        )
    if case == "closed":
        await db.execute(
            text(
                "UPDATE delay_events SET closed_at = :t, close_reason = 'RECEIVED'"
                " WHERE delay_event_id = :e"
            ),
            {"t": DECISION_NOW, "e": run.delay_event_id},
        )
    if case == "not_latest":
        await db.execute(
            text(
                "UPDATE analysis_runs SET is_latest = false WHERE analysis_run_id = :r"
            ),
            {"r": run.analysis_run_id},
        )

    assert not await outcome_save._stale_if_operating_rule_not_active(db, started)

    assert (await _stale_of(db, run)).status == "READY_FOR_REVIEW"


async def test_worker_role_can_save_stale_on_rule_check(db_write_session):
    """분석 실행기의 워커 역할(행 보안 적용)로 활성 버전을 읽고 실행을 STALE 로 바꿀 수 있어야 한다."""
    db = db_write_session
    run = await _ready_run(db)
    old = await _rule_of(db, run)
    await _finish_on_decision_day(db, run)
    await _new_effective_version(db, run)
    await set_rls_context(db, user_id=None, company_id=run.company_id)
    await db.execute(text("SET LOCAL ROLE glowship_v4_worker"))

    assert await outcome_save._stale_if_operating_rule_not_active(
        db, _started(run, old.operating_rule_set_id)
    )

    assert (await _stale_of(db, run)).status == "STALE"
