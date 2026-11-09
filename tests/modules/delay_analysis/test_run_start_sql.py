import uuid
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.modules.delay_analysis.application.agent.fake_runtime import (
    FakeScenario,
    fake_runtime,
)
from app.modules.delay_analysis.application.agent.state import AnalysisStep
from app.modules.delay_analysis.application.result_save.result_rows import (
    ResultSaveInputError,
)
from app.modules.delay_analysis.application.result_save.result_save_service import (
    save_no_delay,
)
from app.modules.delay_analysis.application.run_start.runner import AnalysisRunner
from app.modules.delay_analysis.application.run_start.transitions import (
    RESTART_FAILURE_MESSAGE,
    advance_status,
    begin_analysis,
    fail_unfinished,
    find_unfinished_runs,
)

"""
    분석 자동 시작의 DB 쪽 (GLOW-128) — 실제 DB(확정 overlay 실행 21건), 끝나면 롤백
      - 확정 DB 실행은 모두 끝난 상태라, 시험마다 실행 하나를 같은 트랜잭션 안에서 진행 중으로 되돌려 쓴다.
          · 지연 해소 실행(E8 v2): 회복으로 닫힌 에피소드, 현재 실행, 데이터 버전 5건, 판정 서명 있음
          · 보류 실행(E9 v1): 입고로 닫힌 에피소드, 현재 실행 아님, 데이터 버전 0건
      - 확인: 시작 전이(DETECTED 만, 닫힌 에피소드·현재 실행 아님도 시작), 단계 전이(앞으로만, STALE 안 덮음),
        정리 FAILED(그때 단계), save_no_delay 왕복(확정 행과 열 단위로 같음), 실행기 끝까지(가짜 묶음 보류 → 실제 저장).
      - DB 가 없거나 해당 실행이 없으면 건너뛴다(tests/conftest.py 픽스처).
"""

_RESET = text(
    "UPDATE analysis_runs SET status = :status, completed_status = NULL, result_class = NULL,"
    " status_reason = NULL, failed_step = NULL, failure_code = NULL, attempt_count = NULL,"
    " retry_exhausted = NULL, failure_message = NULL, dependency_scope = NULL,"
    " decision_signature = NULL, decision_signature_sha256 = NULL, finished_at = NULL"
    " WHERE analysis_run_id = :id"
)
_ROW = text(
    "SELECT status, completed_status, result_class, status_reason, failed_step, failure_code,"
    " attempt_count, retry_exhausted, failure_message, dependency_scope, decision_signature,"
    " decision_signature_sha256, model_name, prompt_version, final_explanation_json,"
    " explanation_validation_status, stale_primary_reason, started_at, finished_at, company_id,"
    " is_latest FROM analysis_runs WHERE analysis_run_id = :id"
)
_DATA_VERSIONS = text(
    "SELECT count(*) FROM analysis_run_data_versions WHERE analysis_run_id = :id"
)


async def _run_of(db, status):
    rid = await db.scalar(
        text(
            "SELECT analysis_run_id FROM analysis_runs WHERE status = :s"
            " ORDER BY analysis_run_id LIMIT 1"
        ),
        {"s": status},
    )
    if rid is None:
        pytest.skip(f"{status} 실행이 없는 DB 라 건너뜀")
    return rid


async def _row(db, rid):
    return (await db.execute(_ROW, {"id": rid})).one()


async def _reset(db, rid, status="DETECTED"):
    await db.execute(_RESET, {"id": rid, "status": status})
    return (await _row(db, rid)).company_id


async def test_begin_starts_detected_run_once(db_write_session):
    db = db_write_session
    rid = await _run_of(db, "NO_DELAY")
    company = await _reset(db, rid)

    first = await begin_analysis(db, company_id=company, analysis_run_id=rid)
    second = await begin_analysis(db, company_id=company, analysis_run_id=rid)

    assert first is not None and first.analysis_run_id == rid
    assert second is None  # 이미 시작함: 두 번 돌지 않음
    assert (await _row(db, rid)).status == "VALIDATING"


async def test_begin_starts_run_of_closed_episode_and_not_latest(db_write_session):
    db = db_write_session
    rid = await _run_of(db, "ON_HOLD")  # 입고로 닫힌 에피소드, 현재 실행 아님
    company = await _reset(db, rid)
    closed = await db.scalar(
        text(
            "SELECT e.closed_at IS NOT NULL FROM analysis_runs r"
            " JOIN delay_events e USING (delay_event_id) WHERE r.analysis_run_id = :id"
        ),
        {"id": rid},
    )

    run = await begin_analysis(db, company_id=company, analysis_run_id=rid)

    assert closed and not (await _row(db, rid)).is_latest
    assert (
        run is not None
    )  # 에피소드 열림·현재 실행을 요구하지 않음(DETECTED 로 남지 않게)


async def test_begin_does_not_start_stale_or_other_company(db_write_session):
    db = db_write_session
    rid = await _run_of(db, "STALE")
    company = (await _row(db, rid)).company_id

    assert await begin_analysis(db, company_id=company, analysis_run_id=rid) is None
    assert (
        await begin_analysis(db, company_id=uuid.uuid4(), analysis_run_id=rid) is None
    )
    assert (await _row(db, rid)).status == "STALE"


async def test_advance_only_forward_and_not_over_stale(db_write_session):
    db = db_write_session
    rid = await _run_of(db, "NO_DELAY")
    company = await _reset(db, rid, "VALIDATING")
    stale = await _run_of(db, "STALE")
    stale_company = (await _row(db, stale)).company_id

    def go(run_id, comp, to):
        return advance_status(db, company_id=comp, analysis_run_id=run_id, to=to)

    assert await go(rid, company, AnalysisStep.ANALYZING_IMPACT) is True
    assert await go(rid, company, AnalysisStep.ANALYZING_IMPACT) is False  # 같은 단계
    assert await go(rid, company, AnalysisStep.VALIDATING) is False  # 뒤로
    assert await go(rid, company, AnalysisStep.VALIDATING_OUTPUT) is True
    assert (await _row(db, rid)).status == "VALIDATING_OUTPUT"
    assert await go(stale, stale_company, AnalysisStep.ANALYZING_IMPACT) is False
    assert (await _row(db, stale)).status == "STALE"


async def test_fail_unfinished_keeps_current_step_and_skips_detected(db_write_session):
    db = db_write_session
    rid = await _run_of(db, "NO_DELAY")
    company = await _reset(db, rid, "ANALYZING_OPTIONS")
    hold = await _run_of(db, "ON_HOLD")
    hold_company = await _reset(db, hold)

    saved = await fail_unfinished(
        db, company_id=company, analysis_run_id=rid, message=RESTART_FAILURE_MESSAGE
    )
    untouched = await fail_unfinished(
        db,
        company_id=hold_company,
        analysis_run_id=hold,
        message=RESTART_FAILURE_MESSAGE,
    )
    row = await _row(db, rid)

    assert saved is not None and saved.saved
    assert (row.status, row.completed_status, row.failed_step, row.failure_code) == (
        "FAILED",
        "FAILED",
        "ANALYZING_OPTIONS",
        "UNEXPECTED_TOOL_ERROR",
    )
    assert (row.attempt_count, row.retry_exhausted, row.failure_message) == (
        1,
        False,
        RESTART_FAILURE_MESSAGE,
    )
    assert untouched is None and (await _row(db, hold)).status == "DETECTED"


async def test_find_unfinished_runs_sees_reset_run(db_write_session):
    db = db_write_session
    rid = await _run_of(db, "ON_HOLD")
    company = await _reset(db, rid, "ANALYZING_IMPACT")

    found = await find_unfinished_runs(db)

    assert (company, rid, "ANALYZING_IMPACT") in [
        (f.company_id, f.analysis_run_id, f.status) for f in found
    ]


async def test_save_no_delay_round_trips_confirmed_row(db_write_session):
    db = db_write_session
    rid = await _run_of(db, "NO_DELAY")
    original = await _row(db, rid)
    company = await _reset(db, rid, "VALIDATING")

    outcome = await save_no_delay(
        db,
        company_id=company,
        analysis_run_id=rid,
        decision_signature=original.decision_signature,
        decision_signature_sha256=original.decision_signature_sha256,
        now=original.started_at,
    )
    after = await _row(db, rid)

    assert outcome.saved and outcome.status == "NO_DELAY"
    # 끝난 시각만 다르다: 확정 DB 는 소요 0초(하네스 finished = confirm_t), 서버는 시작 + 1초(이슈 #39 규칙 3)
    assert {**after._asdict(), "finished_at": None} == {
        **original._asdict(),
        "finished_at": None,
    }
    assert after.finished_at == original.started_at + timedelta(seconds=1)
    for table in (
        "inventory_impact_results",
        "at_risk_orders",
        "response_options",
    ):
        assert (
            await db.scalar(
                text(f"SELECT count(*) FROM {table} WHERE analysis_run_id = :id"),
                {"id": rid},
            )
            == 0
        )


async def test_save_no_delay_rejects_bad_signature_and_keeps_stale(db_write_session):
    db = db_write_session
    rid = await _run_of(db, "NO_DELAY")
    original = await _row(db, rid)
    company = await _reset(db, rid, "VALIDATING")
    bad = dict(original.decision_signature, options=[{"x": 1}])

    with pytest.raises(ResultSaveInputError):
        await save_no_delay(
            db,
            company_id=company,
            analysis_run_id=rid,
            decision_signature=bad,
            decision_signature_sha256=original.decision_signature_sha256,
        )
    with pytest.raises(ResultSaveInputError):
        await save_no_delay(
            db,
            company_id=company,
            analysis_run_id=rid,
            decision_signature=original.decision_signature,
            decision_signature_sha256="ABC",
        )
    assert (await _row(db, rid)).status == "VALIDATING"

    stale = await _run_of(db, "STALE")
    stale_company = (await _row(db, stale)).company_id
    kept = await save_no_delay(
        db,
        company_id=stale_company,
        analysis_run_id=stale,
        decision_signature=original.decision_signature,
        decision_signature_sha256=original.decision_signature_sha256,
    )
    assert not kept.saved and (await _row(db, stale)).status == "STALE"


async def test_runner_end_to_end_hold_saves_without_data_versions(db_write_session):
    """가짜 묶음(보류) → 실제 시작 전이·저장. 보류는 데이터 버전을 기록하지 않는다(확정 DB 와 같음)."""
    db = db_write_session
    rid = await _run_of(db, "ON_HOLD")
    company = await _reset(db, rid)

    @asynccontextmanager
    async def same_session():
        yield db

    runner = AnalysisRunner(
        open_session=same_session,
        runtime_factory=lambda s, r: fake_runtime(
            FakeScenario(hold_code="REQUIRED_DATA_INVALID")
        ),
        save_time=lambda run: run.started_at,
    )
    assert runner.start(company, rid)
    await runner.wait_idle()
    row = await _row(db, rid)

    assert (row.status, row.completed_status, row.failed_step, row.failure_code) == (
        "ON_HOLD",
        "ON_HOLD",
        "VALIDATING",
        "REQUIRED_DATA_INVALID",
    )
    assert row.finished_at == row.started_at + timedelta(seconds=1)
    assert await db.scalar(_DATA_VERSIONS, {"id": rid}) == 0


async def test_runner_end_to_end_failed_records_data_versions(db_write_session):
    """보류가 아닌 종료(여기서는 도구 밖 예외 FAILED)는 입력 데이터 버전을 기록한다(재분석 실행 포함)."""
    db = db_write_session
    rid = await _run_of(db, "NO_DELAY")
    company = await _reset(db, rid)
    await db.execute(
        text("DELETE FROM analysis_run_data_versions WHERE analysis_run_id = :id"),
        {"id": rid},
    )

    def broken_runtime(_session, _run):
        runtime = fake_runtime()

        async def broken():
            raise RuntimeError("엔진 입력 DB 오류(시험)")

        runtime.preparer.judge_no_delay = broken
        return runtime

    @asynccontextmanager
    async def same_session():
        yield db

    runner = AnalysisRunner(open_session=same_session, runtime_factory=broken_runtime)
    runner.start(company, rid)
    await runner.wait_idle()
    row = await _row(db, rid)

    assert (row.status, row.failed_step, row.failure_code) == (
        "FAILED",
        "VALIDATING",
        "UNEXPECTED_TOOL_ERROR",
    )
    assert await db.scalar(_DATA_VERSIONS, {"id": rid}) == 5
