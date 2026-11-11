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
    RetireOutcome,
    advance_status,
    begin_analysis,
    fail_unfinished,
    find_unfinished_runs,
    retire_unstarted_run_of_closed_episode,
)

"""
    분석 자동 시작의 DB 쪽 (GLOW-128) — 실제 DB(확정 overlay 실행 21건), 끝나면 롤백
      - 확정 DB 실행은 모두 끝난 상태라, 시험마다 실행 하나를 같은 트랜잭션 안에서 진행 중으로 되돌려 쓴다.
          · 지연 해소 실행(E8 v2): 회복으로 닫힌 에피소드, 현재 실행, 데이터 버전 5건, 판정 서명 있음
          · 보류 실행(E9 v1): 입고로 닫힌 에피소드, 현재 실행 아님, 데이터 버전 0건
      - 확인: 시작 전이(DETECTED 만, 닫힌 에피소드·현재 실행 아님도 시작), 단계 전이(앞으로만, STALE 안 덮음),
        정리 FAILED(그때 단계), save_no_delay 왕복(확정 행과 열 단위로 같음), 실행기 끝까지(가짜 묶음 보류 → 실제 저장),
        시작 정리의 닫힌 지연 처리(이슈 #51: 비회복 실행은 STALE, 회복 실행·열린 지연은 시작, 닫은 수집 못 찾으면 그대로).
      - 닫힌 지연 시험의 기대 순번·시각은 구현과 다른 길로 구한다: 회복은 회복 실행 원인 스냅샷의 수집,
        입고는 그 선적이 처음 RECEIVED 로 관측된 스냅샷의 수집.
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


async def test_fail_unfinished_records_data_versions_like_graph_failure(
    db_write_session,
):
    """
    그래프 밖에서 끝내는 FAILED(마지막 잡기·시작 정리)도 입력 데이터 버전을 기록한다(사용자 결정 11-10).
    재분석 실행처럼 만들 때 기록하지 않은 실행을 흉내 내려고 지운 뒤 부른다. DETECTED 는 그대로 0건.
    """
    db = db_write_session
    rid = await _run_of(db, "NO_DELAY")
    company = await _reset(db, rid, "ANALYZING_IMPACT")
    hold = await _run_of(db, "ON_HOLD")
    hold_company = await _reset(db, hold)
    for run_id in (rid, hold):
        await db.execute(
            text("DELETE FROM analysis_run_data_versions WHERE analysis_run_id = :id"),
            {"id": run_id},
        )

    await fail_unfinished(
        db, company_id=company, analysis_run_id=rid, message=RESTART_FAILURE_MESSAGE
    )
    await fail_unfinished(
        db,
        company_id=hold_company,
        analysis_run_id=hold,
        message=RESTART_FAILURE_MESSAGE,
    )

    assert (await _row(db, rid)).status == "FAILED"
    assert await db.scalar(_DATA_VERSIONS, {"id": rid}) == 5
    assert await db.scalar(_DATA_VERSIONS, {"id": hold}) == 0


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
        runtime_factory=lambda s, r, t: fake_runtime(
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

    def broken_runtime(_session, _run, _tracer):
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


# --- 시작 정리: 닫힌 지연의 시작 전 실행 (이슈 #51, 사용자 결정 11-11) ---------------------------------

_RESET_DETECTED = text(
    "UPDATE analysis_runs SET status = 'DETECTED', completed_status = NULL, result_class = NULL,"
    " status_reason = NULL, failed_step = NULL, failure_code = NULL, attempt_count = NULL,"
    " retry_exhausted = NULL, failure_message = NULL, dependency_scope = NULL,"
    " decision_signature = NULL, decision_signature_sha256 = NULL, finished_at = NULL,"
    " stale_primary_reason = NULL, stale_reason_codes = NULL, stale_detected_seq = NULL,"
    " stale_detected_at = NULL"
    " WHERE analysis_run_id = :id"
)
_STALE_ROW = text(
    "SELECT status, completed_status, stale_primary_reason, stale_reason_codes,"
    " stale_detected_seq, stale_detected_at, finished_at FROM analysis_runs"
    " WHERE analysis_run_id = :id"
)
# 회복으로 닫힌 지연: 회복 실행(DELAY_RECOVERED)의 원인 스냅샷을 남긴 수집이 지연을 닫은 수집
_RECOVERED_CASE = text(
    "SELECT p.analysis_run_id AS run_id, rec.analysis_run_id AS recovery_id,"
    " d.applied_seq, d.finished_at"
    " FROM analysis_runs rec"
    " JOIN delay_events e ON e.delay_event_id = rec.delay_event_id AND e.close_reason = 'RECOVERED'"
    " JOIN analysis_runs p ON p.delay_event_id = rec.delay_event_id"
    "  AND p.trigger_reason <> 'DELAY_RECOVERED'"
    " JOIN shipment_schedule_snapshots s"
    "  ON s.shipment_schedule_snapshot_id = rec.trigger_shipment_schedule_snapshot_id"
    " JOIN data_sync_runs d ON d.data_sync_run_id = s.data_sync_run_id"
    " WHERE rec.trigger_reason = 'DELAY_RECOVERED'"
    " ORDER BY p.version_no LIMIT 1"
)
# 입고로 닫힌 지연: 그 선적이 처음 RECEIVED 로 관측된 스냅샷을 남긴 수집
_RECEIVED_CASE = text(
    "SELECT r.analysis_run_id AS run_id, r.delay_event_id, d.applied_seq, d.finished_at"
    " FROM analysis_runs r"
    " JOIN delay_events e ON e.delay_event_id = r.delay_event_id AND e.close_reason = 'RECEIVED'"
    " JOIN LATERAL (SELECT s.data_sync_run_id FROM shipment_schedule_snapshots s"
    "  JOIN data_sync_runs ds ON ds.data_sync_run_id = s.data_sync_run_id"
    "  WHERE s.shipment_id = e.shipment_id AND s.stage = 'RECEIVED'"
    "  ORDER BY ds.applied_seq LIMIT 1) first_received ON true"
    " JOIN data_sync_runs d ON d.data_sync_run_id = first_received.data_sync_run_id"
    " WHERE r.trigger_reason <> 'DELAY_RECOVERED'"
    " ORDER BY r.analysis_run_id LIMIT 1"
)


async def _case(db, query):
    row = (await db.execute(query)).first()
    if row is None:
        pytest.skip("닫힌 지연 실행이 없는 DB 라 건너뜀")
    return row


async def _detected(db, rid):
    """DETECTED 로 되돌리고 저장점을 커밋한다(시작 정리가 조회 뒤 rollback 해도 준비가 남게, 끝나면 바깥 롤백)."""
    await db.execute(_RESET_DETECTED, {"id": rid})
    company = (await _row(db, rid)).company_id
    await db.commit()
    return company


def _sweeper(db) -> AnalysisRunner:
    @asynccontextmanager
    async def same_session():
        yield db

    return AnalysisRunner(
        open_session=same_session,
        runtime_factory=lambda s, r, t: fake_runtime(
            FakeScenario(hold_code="REQUIRED_DATA_INVALID")
        ),
        save_time=lambda run: run.started_at,
    )


async def test_sweep_retires_unstarted_run_of_received_episode(db_write_session):
    """입고로 닫힌 지연의 비회복 DETECTED → 시작하지 않고 STALE(DELAY_STAGE_CHANGED, 닫은 수집)."""
    db = db_write_session
    case = await _case(db, _RECEIVED_CASE)
    await _detected(db, case.run_id)
    runner = _sweeper(db)

    result = await runner.sweep_on_startup()
    await runner.wait_idle()
    row = (await db.execute(_STALE_ROW, {"id": case.run_id})).one()

    assert (result.retired, result.restarted, result.errors) == ([case.run_id], [], [])
    assert (row.status, row.completed_status, row.finished_at) == ("STALE", None, None)
    assert (row.stale_primary_reason, row.stale_reason_codes) == (
        "DELAY_STAGE_CHANGED",
        ["DELAY_STAGE_CHANGED"],
    )
    assert (row.stale_detected_seq, row.stale_detected_at) == (
        case.applied_seq,
        case.finished_at,
    )


async def test_sweep_retires_unstarted_run_of_recovered_episode(db_write_session):
    """회복으로 닫힌 지연(회복 실행 생성을 건너뛴 경우)의 비회복 DETECTED → STALE(DELAY_SCHEDULE_CHANGED)."""
    db = db_write_session
    case = await _case(db, _RECOVERED_CASE)
    await _detected(db, case.run_id)
    runner = _sweeper(db)

    result = await runner.sweep_on_startup()
    await runner.wait_idle()
    row = (await db.execute(_STALE_ROW, {"id": case.run_id})).one()

    assert result.retired == [case.run_id] and result.restarted == []
    assert (row.status, row.stale_primary_reason) == ("STALE", "DELAY_SCHEDULE_CHANGED")
    assert (row.stale_detected_seq, row.stale_detected_at) == (
        case.applied_seq,
        case.finished_at,
    )


async def test_sweep_still_starts_recovery_run_of_closed_episode(db_write_session):
    """음성 대조: 회복 실행(DELAY_RECOVERED)은 닫힌 에피소드에서도 그대로 시작한다."""
    db = db_write_session
    case = await _case(db, _RECOVERED_CASE)
    await _detected(db, case.recovery_id)
    runner = _sweeper(db)

    result = await runner.sweep_on_startup()
    await runner.wait_idle()

    assert result.restarted == [case.recovery_id] and result.retired == []
    assert (
        await _row(db, case.recovery_id)
    ).status == "ON_HOLD"  # 가짜 묶음(보류)까지 돎


async def test_sweep_starts_detected_run_of_open_episode(db_write_session):
    """열린 지연의 DETECTED 는 지금처럼 시작한다(확정 DB 는 지연이 모두 닫혀 있어 트랜잭션 안에서 다시 엶)."""
    db = db_write_session
    case = await _case(db, _RECEIVED_CASE)
    await _detected(db, case.run_id)
    await db.execute(
        text(
            "UPDATE delay_events SET closed_at = NULL, close_reason = NULL"
            " WHERE delay_event_id = :id"
        ),
        {"id": case.delay_event_id},
    )
    await db.commit()
    runner = _sweeper(db)

    result = await runner.sweep_on_startup()
    await runner.wait_idle()

    assert result.restarted == [case.run_id] and result.retired == []
    assert (await _row(db, case.run_id)).status == "ON_HOLD"


async def test_sweep_leaves_run_when_closing_sync_is_unknown(db_write_session):
    """닫은 수집을 찾지 못하면(닫은 시각과 같은 관측이 없음) 바꾸지도 시작하지도 않는다."""
    db = db_write_session
    case = await _case(db, _RECEIVED_CASE)
    await _detected(db, case.run_id)
    await db.execute(
        text(
            "UPDATE delay_events SET closed_at = closed_at + interval '1 second'"
            " WHERE delay_event_id = :id"
        ),
        {"id": case.delay_event_id},
    )
    await db.commit()
    runner = _sweeper(db)

    result = await runner.sweep_on_startup()
    await runner.wait_idle()

    assert result.left_detected == [case.run_id]
    assert result.restarted == [] and result.retired == []
    assert (await _row(db, case.run_id)).status == "DETECTED"


async def test_retire_under_worker_role_and_rls(db_write_session):
    """워커 역할(행 보안 적용)로도 닫힌 지연·스냅샷·수집을 읽고 STALE 을 쓴다(마이그레이션 없이)."""
    db = db_write_session
    case = await _case(db, _RECEIVED_CASE)
    company = await _detected(db, case.run_id)
    await db.execute(text("SET LOCAL ROLE glowship_v4_worker"))

    other = await retire_unstarted_run_of_closed_episode(
        db, company_id=uuid.uuid4(), analysis_run_id=case.run_id
    )
    outcome = await retire_unstarted_run_of_closed_episode(
        db, company_id=company, analysis_run_id=case.run_id
    )
    row = (await db.execute(_STALE_ROW, {"id": case.run_id})).one()

    assert outcome == RetireOutcome.RETIRED and other == RetireOutcome.NOT_TARGET
    assert (row.status, row.stale_detected_seq) == ("STALE", case.applied_seq)
