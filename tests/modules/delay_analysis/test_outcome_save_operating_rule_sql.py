import uuid
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
