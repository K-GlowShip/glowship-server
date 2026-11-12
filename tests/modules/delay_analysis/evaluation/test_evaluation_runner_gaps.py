from dataclasses import replace
from datetime import timedelta

from real_path_support import (
    ROW_PARTS,
    SNAPSHOT_COLUMNS,
    WORKER_ROLE,
    answer_snapshot,
    app_like_runner,
    differences,
    fix_explanation_canary,
    pick_runs,
    require_engine,
    rerun,
    reset_to_detected,
    run_to_end,
    state_problems,
    worker_role,
)
from sqlalchemy import text

from app.modules.data_sync.application.ingest.ingest_service import DataIngestService
from app.modules.data_sync.infrastructure.data_sync_enums import (
    ConnectionMethod,
    DataSyncRunStatus,
    SourceDomain,
)
from app.modules.data_sync.infrastructure.repository.ingest_repository import (
    IngestRepository,
)
from app.modules.delay_analysis.application.agent.graph import (
    UNEXPECTED_ERROR_MESSAGE,
)
from app.modules.delay_analysis.application.agent.state import AnalysisOutcome
from app.modules.delay_analysis.application.agent.tools.common import ToolName
from app.modules.delay_analysis.application.agent.tools.registry import ToolRegistry
from app.modules.delay_analysis.application.run_start.transitions import (
    RESTART_FAILURE_MESSAGE,
)
from app.modules.delay_analysis.application.stale.stale_service import (
    StaleJudgmentService,
)
from app.modules.delay_analysis.infrastructure.engine.signature_recalculator import (
    recalculator_or_none,
)
from app.modules.delay_analysis.infrastructure.repository.stale_change_repository import (
    StaleChangeRepository,
)
from tests.modules.data_sync.test_ingest_episode_sql import (
    DAY1,
    DAY2,
    _delayed_new_shipment,
)
from tests.modules.delay_analysis.test_result_save import _snapshot

"""
    실행기의 빈 곳을 실제 DB·실제 묶음으로 (GLOW-58 2단계 B 갈래, docs/analysis/auto-start.md 7절 "시험이 비는 곳")
      - B1 재분석 실행은 만들 때 입력 데이터 버전을 기록하지 않는다 → 저장 때 5건이 채워진다.
      - B2 실제 묶음을 만들다 예외가 나면 실행기의 마지막 잡기가 FAILED(VALIDATING·UNEXPECTED_TOOL_ERROR)로 남긴다.
      - B3 서버 시작 정리(워커 역할, 리비전 0009 함수): DETECTED 는 다시 시작해 끝까지, 분석 도중이던 실행은 그 단계 FAILED.
      - B4 도는 중 다른 트랜잭션이 STALE 로 바꾼 실행은 노드 경계에서 멈추고, 저장하지 않아 STALE 그대로다.
      - B5 수집에서 저장까지 한 흐름: 새 지연 선적 수집 → v1 자동 실행 → 실제 경로 → 일정 악화 수집 →
        v1 STALE·v2 자동 실행 → 실제 경로. 조각(자동 실행 생성 test_auto_run_sql, 수집 → 시작 넘김
        test_manual_sync_start_runs)은 이미 시험이 있고, 여기서는 실제 STALE 판정·실제 묶음으로 한 번 이어 붙인다.
        수집이 돌려준 실행 ID 를 실행기에 직접 넘긴다(수동 수집의 넘김은 거치지 않음).
        새 선적은 지연 증분이 없어 두 실행 모두 NO_RISK 로 끝난다.
      - 모두 롤백 세션 하나로 돈다(real_path_support). 분석은 워커 역할이다.
"""

T1 = ToolName.GET_DELAY_CONTEXT
REANALYSIS = {("A_BEAUTY", "AN-260102-001"): "READY_FOR_REVIEW"}
NO_DELAY = {("B_COSMETICS", "AN-260102-001"): "NO_DELAY"}
ON_HOLD = {("B_COSMETICS", "AN-260102-002"): "ON_HOLD"}
READY_B = {("B_COSMETICS", "AN-260105-001"): "READY_FOR_REVIEW"}

_DATA_VERSIONS = text(
    "SELECT data_sync_run_id FROM analysis_run_data_versions"
    " WHERE analysis_run_id = :id ORDER BY data_sync_run_id"
)
_SET_STATUS = text(
    "UPDATE analysis_runs SET status = :status WHERE analysis_run_id = :id"
)
_MAKE_STALE = text(
    "UPDATE analysis_runs SET status = 'STALE', stale_primary_reason = 'TIME_ELAPSED',"
    " stale_reason_codes = ARRAY['TIME_ELAPSED'], stale_detected_at = now()"
    " WHERE analysis_run_id = :id"
)
_CHAIN_RUNS = text(
    """
    SELECT r.analysis_run_id, r.version_no, r.trigger_type, r.trigger_reason, r.status, r.completed_status,
           r.is_latest, r.input_seq, r.stale_primary_reason, r.stale_detected_seq, r.parent_analysis_run_id,
           (SELECT count(*) FROM analysis_run_data_versions v WHERE v.analysis_run_id = r.analysis_run_id) AS versions,
           (SELECT count(*) FROM inventory_impact_results i WHERE i.analysis_run_id = r.analysis_run_id) AS impacts
      FROM analysis_runs r
      JOIN delay_events e USING (delay_event_id)
      JOIN shipments s ON s.shipment_id = e.shipment_id
     WHERE r.company_id = :company AND s.shipment_code = :code
     ORDER BY r.version_no
    """
)


# 결과를 저장한 뒤의 입력 데이터 버전 수(도메인 5개). 자동 실행은 만들 때가 아니라 결과를 저장할 때 기록한다(#120).
# 만든 직후 0건인 것은 test_auto_run_sql 이 본다
VERSIONS_AFTER_SAVE = 5


async def _versions(db, run):
    return (await db.scalars(_DATA_VERSIONS, {"id": run.analysis_run_id})).all()


async def _row(db, run):
    snapshot = await _snapshot(db, run.analysis_run_id, SNAPSHOT_COLUMNS)
    return snapshot["run"], {part: len(snapshot[part]) for part in ROW_PARTS}


def _five(row):
    return (
        row["status"],
        row["completed_status"],
        row["failed_step"],
        row["failure_code"],
        row["attempt_count"],
        row["retry_exhausted"],
        row["failure_message"],
    )


async def test_reanalysis_run_gets_data_versions_at_save(db_write_session, monkeypatch):
    """B1: 재분석 실행의 데이터 버전 5건을 지우고 실제 경로로 → 저장 때 원래와 같은 5건."""
    require_engine()
    db = db_write_session
    (run,) = await pick_runs(db, REANALYSIS)
    fix_explanation_canary(monkeypatch)
    answer = await answer_snapshot(db, run)
    original = await _versions(db, run)
    await db.execute(
        text("DELETE FROM analysis_run_data_versions WHERE analysis_run_id = :id"),
        {"id": run.analysis_run_id},
    )

    result = await rerun(db, run)

    assert len(original) == 5
    assert await _versions(db, run) == original
    assert differences(answer, result.after) + state_problems(result) == []


async def test_runtime_wiring_error_is_saved_as_failed_by_last_catch(db_write_session):
    """B2: 실제 묶음을 만들다 예외 → 마지막 잡기가 지금 단계 FAILED 로 남기고 결과 행은 없다."""
    db = db_write_session
    (run,) = await pick_runs(db, NO_DELAY)
    states: list = []

    def broken(_runtime, _run):
        raise RuntimeError("실제 묶음 연결 실수(시험)")

    await reset_to_detected(db, run)
    runner = app_like_runner(db, tune=broken, states=states)
    await run_to_end(db, runner, [(run.company_id, run.analysis_run_id)])
    row, counts = await _row(db, run)

    assert states == []  # 그래프 종료 상태 저장까지 가지 않음
    assert _five(row) == (
        "FAILED",
        "FAILED",
        "VALIDATING",
        "UNEXPECTED_TOOL_ERROR",
        1,
        False,
        UNEXPECTED_ERROR_MESSAGE,
    )
    assert row["finished_at"] == row["started_at"] + timedelta(seconds=1)
    assert counts == dict.fromkeys(ROW_PARTS, 0)


async def test_startup_sweep_restarts_detected_and_fails_in_progress(db_write_session):
    """B3: 워커 역할의 서버 시작 정리. DETECTED 는 실제 경로로 끝까지, 분석 도중이던 실행은 그 단계 FAILED."""
    require_engine()
    db = db_write_session
    (detected,) = await pick_runs(db, NO_DELAY)
    (halfway,) = await pick_runs(db, ON_HOLD)
    answer = await answer_snapshot(db, detected)
    await reset_to_detected(db, detected)
    await reset_to_detected(db, halfway)
    await db.execute(
        _SET_STATUS, {"status": "ANALYZING_IMPACT", "id": halfway.analysis_run_id}
    )
    await db.commit()
    runner = app_like_runner(db)
    # 롤백 세션 하나는 동시에 쓸 수 없다(앱은 실행마다 접속이 따로). 그래서 정리가 다시 시작시키는 실행은
    # 받아 두었다가 정리가 끝난 뒤에 시작한다. 무엇을 다시 시작하고 무엇을 FAILED 로 바꾸는지는 실행기가 정한다.
    start, deferred = runner.start, []
    runner.start = lambda company_id, run_id: (
        deferred.append((company_id, run_id)) or True
    )

    async with worker_role(db):
        swept = await runner.sweep_on_startup()
        runner.start = start
        for company_id, run_id in deferred:
            assert runner.start(company_id, run_id)
            await runner.wait_idle()
    db.expire_all()
    restarted = await _snapshot(db, detected.analysis_run_id, SNAPSHOT_COLUMNS)
    row, counts = await _row(db, halfway)

    assert detected.analysis_run_id in swept.restarted
    assert halfway.analysis_run_id in swept.failed and swept.errors == []
    assert differences(answer, restarted) == []  # 다시 시작해 정답(NO_DELAY) 그대로
    assert restarted["run"]["status"] == "NO_DELAY"
    assert _five(row) == (
        "FAILED",
        "FAILED",
        "ANALYZING_IMPACT",
        "UNEXPECTED_TOOL_ERROR",
        1,
        False,
        RESTART_FAILURE_MESSAGE,
    )
    assert counts == dict.fromkeys(ROW_PARTS, 0)


async def test_run_made_stale_while_running_stops_and_is_not_saved(db_write_session):
    """B4: T1 뒤에 다른 트랜잭션이 STALE 로 바꾼 것처럼 → 노드 경계에서 멈추고 STALE 을 덮지 않는다."""
    require_engine()
    db = db_write_session
    (run,) = await pick_runs(db, READY_B)

    def stale_after_t1(runtime, _run):
        tools = dict(runtime.registry)
        spec = tools[T1]

        async def then_stale(ctx, payload):
            output = await spec.fn(ctx, payload)
            await db.execute(_MAKE_STALE, {"id": run.analysis_run_id})
            await (
                db.commit()
            )  # 다른 트랜잭션이 커밋한 것과 같게(그래프 세션 rollback 에 지워지지 않게)
            return output

        tools[T1] = replace(spec, fn=then_stale)
        runtime.registry = ToolRegistry(tools)

    result = await rerun(db, run, tune=stale_after_t1)
    row, counts = await _row(db, run)

    assert result.state["outcome"] == AnalysisOutcome.STALE
    assert result.saved == [None] and result.roles == [WORKER_ROLE]  # 저장하지 않음
    assert (row["status"], row["completed_status"], row["finished_at"]) == (
        "STALE",
        None,
        None,
    )
    assert row["stale_primary_reason"] == "TIME_ELAPSED"
    assert counts == dict.fromkeys(ROW_PARTS, 0)


async def _ingest(db, at, company_id, shipments):
    """
    실제 STALE 판정·실제 자동 실행 생성을 쓰는 수집(수집 시험의 보조는 STALE 판정을 대역으로 바꿔 넣는다).
    STALE 2차(판정 서명 재계산)는 수동 동기화(manual_sync_service._ingest)와 같게 recalculator_or_none 을 넘긴다(#116).
    """
    service = DataIngestService(
        db,
        IngestRepository(db),
        StaleJudgmentService(db, StaleChangeRepository(db), recalculator_or_none(db)),
        now=lambda: at,
    )
    result = await service.ingest(
        company_id, SourceDomain.SHIPMENT, ConnectionMethod.API, shipments
    )
    assert result.status == DataSyncRunStatus.SUCCEEDED, result.rejections
    return result


async def _analyze(db, company_id, run_id):
    states: list = []
    await run_to_end(db, app_like_runner(db, states=states), [(company_id, run_id)])
    return states[-1]["outcome"]


async def _chain(db, company_id, shipment_code):
    rows = await db.execute(_CHAIN_RUNS, {"company": company_id, "code": shipment_code})
    return rows.all()


async def test_ingest_to_saved_result_in_one_flow(db_write_session, monkeypatch):
    """B5: 수집 → 자동 실행 → 실제 경로 → 일정 악화 수집 → 이전 실행 STALE·후속 실행 → 실제 경로."""
    require_engine()
    db = db_write_session
    (anchor,) = await pick_runs(db, NO_DELAY)  # 작은 고객사
    company_id = anchor.company_id
    fix_explanation_canary(monkeypatch)
    shipment = await _delayed_new_shipment(db, company_id)

    # ① 새 지연 선적 수집 → v1 자동 실행(DETECTED) → 실제 경로로 끝 상태 저장
    first = await _ingest(db, DAY1, company_id, [shipment])
    (v1_created,) = await _chain(db, company_id, shipment.shipment_code)
    await _analyze(db, company_id, first.created_run_ids[0])
    (v1_done,) = await _chain(db, company_id, shipment.shipment_code)

    assert first.created_run_ids == (v1_created.analysis_run_id,)
    assert (v1_created.trigger_type, v1_created.trigger_reason, v1_created.status) == (
        "AUTO",
        "EPISODE_STARTED",
        "DETECTED",
    )
    assert v1_created.input_seq == first.applied_seq
    assert v1_done.status == v1_done.completed_status == "NO_RISK"
    assert v1_done.versions == VERSIONS_AFTER_SAVE

    # ② 같은 선적의 예상 입고일을 늦춰 다시 수집 → v1 STALE, v2 자동 실행
    later = replace(
        shipment,
        current_expected_3pl_inbound_date=shipment.current_expected_3pl_inbound_date
        + timedelta(days=10),
    )
    second = await _ingest(db, DAY2, company_id, [later])
    v1_stale, v2_created = await _chain(db, company_id, shipment.shipment_code)

    assert (v1_stale.status, v1_stale.stale_primary_reason, v1_stale.is_latest) == (
        "STALE",
        "DELAY_SCHEDULE_CHANGED",
        False,
    )
    assert v1_stale.stale_detected_seq == second.applied_seq
    assert v1_stale.completed_status == v1_done.completed_status  # 결과는 보존
    assert v1_stale.impacts == v1_done.impacts
    # ③ v2: 일정 악화 사유, 부모 = v1, 입력 순번 = 그 수집
    assert second.created_run_ids == (v2_created.analysis_run_id,)
    assert (v2_created.trigger_type, v2_created.trigger_reason, v2_created.status) == (
        "AUTO",
        "SCHEDULE_WORSENED",
        "DETECTED",
    )
    assert v2_created.parent_analysis_run_id == v1_stale.analysis_run_id
    assert v2_created.version_no == 2 and v2_created.is_latest
    assert v2_created.input_seq == second.applied_seq

    # ④ v2 실제 경로 → 끝 상태 저장, v1 은 그대로 STALE
    await _analyze(db, company_id, v2_created.analysis_run_id)
    v1_after, v2_done = await _chain(db, company_id, shipment.shipment_code)

    assert v2_done.status == v2_done.completed_status == "NO_RISK"
    assert v2_done.versions == VERSIONS_AFTER_SAVE
    assert (v1_after.status, v1_after.stale_detected_seq) == (
        "STALE",
        second.applied_seq,
    )
