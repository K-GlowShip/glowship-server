from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.modules.delay_analysis.application.stale.stale_service import (
    StaleJudgmentService,
)
from app.modules.delay_analysis.infrastructure.analysis_run import AnalysisRun
from app.modules.delay_analysis.infrastructure.engine.signature_recalculator import (
    EngineSignatureRecalculator,
)
from app.modules.delay_analysis.infrastructure.repository.stale_change_repository import (
    StaleChangeRepository,
)

"""
    판정 서명 재계산기 정답 대조 (이슈 #75) — 실제 DB·실제 엔진, 끝나면 롤백
      - 확정 데이터의 STALE 시점은 엔진이 서명을 다시 계산해 정한 정답이다(엔진 stale.judge).
        B_COSMETICS AN-260103-002(READY_FOR_REVIEW, 입력 순번 20)는 순번 23 의 주문 변경이 의존 범위에 걸리지만
        서명은 같고, 순번 28 에서 서명이 바뀌어 STALE(RELEVANT_ORDER_CHANGED)이다.
      - 실행을 STALE 전 상태로 되돌린 뒤 결과 저장 재판정(recheck_run)을 돌린다. 확정 데이터에서는 뒤 버전(v3·v4)이 생기고
        에피소드가 01-22 에 닫혀 재판정 대상이 아니므로, 롤백되는 이 트랜잭션 안에서 v2 를 현재 버전·에피소드를 열린 것으로 둔다.
        재계산기 없이는 23 에서 STALE(놓치지 않는 쪽), 재계산기로는 27 까지 그대로이고 28 에서 STALE.
      - 확정 STALE 실행 11건 전체 대조는 PR 본문 기록(모두 일치). 한 건에 수십 초가 걸려 시험에는 한 건만 둔다.
      - 엔진 경로나 확정 데이터가 없으면 건너뛴다(CI 는 빈 DB·엔진 없음).
"""

DETECTED_AT = datetime(2026, 1, 9, tzinfo=UTC)


async def _reset_run(db):
    if get_settings().glowship_engine_dir is None:
        pytest.skip("GLOWSHIP_ENGINE_DIR 이 없어 실제 엔진 시험을 건너뜀")
    run_id = await db.scalar(
        text(
            "SELECT a.analysis_run_id FROM analysis_runs a JOIN companies c USING (company_id)"
            " WHERE c.company_code = 'B_COSMETICS' AND a.run_code = 'AN-260103-002'"
        )
    )
    run = None if run_id is None else await db.get(AnalysisRun, run_id)
    if run is None or (run.status, run.input_seq, run.stale_detected_seq) != (
        "STALE",
        20,
        28,
    ):
        pytest.skip(
            "확정 데이터(B_COSMETICS AN-260103-002, 23 무관·28 STALE)가 아닌 DB"
        )
    await db.execute(
        text(
            "UPDATE analysis_runs SET is_latest = false WHERE delay_event_id = :e AND is_latest"
        ),
        {"e": run.delay_event_id},
    )
    await db.execute(
        text(
            "UPDATE delay_events SET closed_at = NULL, close_reason = NULL WHERE delay_event_id = :e"
        ),
        {"e": run.delay_event_id},
    )
    run.is_latest = True
    run.status = run.completed_status  # READY_FOR_REVIEW
    run.stale_primary_reason = None
    run.stale_reason_codes = None
    run.stale_detected_seq = None
    run.stale_detected_at = None
    await db.flush()
    return run


async def _recheck(db, run, up_to_seq, recalculator):
    return await StaleJudgmentService(
        db, StaleChangeRepository(db), recalculator
    ).recheck_run(run.analysis_run_id, DETECTED_AT, up_to_seq=up_to_seq)


async def test_without_recalculator_first_relevant_change_marks_stale(
    db_write_session,
):
    db = db_write_session
    run = await _reset_run(db)

    assert await _recheck(db, run, 27, None) is run
    assert (run.status, run.stale_detected_seq) == ("STALE", 23)  # 정답(28)보다 이름


async def test_recalculator_keeps_run_until_signature_changes(db_write_session):
    db = db_write_session
    run = await _reset_run(db)
    recalculator = EngineSignatureRecalculator(db)

    assert await _recheck(db, run, 27, recalculator) is None
    assert run.status == "READY_FOR_REVIEW"

    assert await _recheck(db, run, 28, recalculator) is run
    assert (run.status, run.stale_detected_seq, run.stale_reason_codes) == (
        "STALE",
        28,
        ["RELEVANT_ORDER_CHANGED"],
    )
