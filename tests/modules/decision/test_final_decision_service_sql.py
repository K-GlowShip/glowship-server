import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.core.security.tenant import TenantContext
from app.modules.decision.application.decision_clock import DecisionClock
from app.modules.decision.application.exception.decision_exceptions import (
    DecisionErrorCode,
    DecisionException,
)
from app.modules.decision.application.final_decision_service import (
    FinalDecisionService,
)
from app.modules.decision.infrastructure.repository.final_decision_repository import (
    FinalDecisionRepository,
)
from app.modules.delay_analysis.application.exception.analysis_run_query_exceptions import (
    AnalysisRunNotFoundException,
)

"""
    최종 선택 서비스 SQL 테스트 (API 19, 실제 DB, 끝나면 롤백)
      - 확정 DB 에는 선택 대기(READY_FOR_REVIEW) 현재 실행이 없다(D12). 롤백 트랜잭션에서 완료 판정이 READY_FOR_REVIEW 인
        현재 STALE 실행을 READY_FOR_REVIEW 로 되돌리고(STALE 사유 비움), 그 에피소드를 다시 열어 선택 대기 상태를 만든다.
      - 시간 경과(TIME_ELAPSED) 경로는 확정 DB 에 회복 주문이 없어 test_final_decision_service.py(대역)가 확인한다.
"""

NOW = datetime(2026, 7, 2, 0, 3, tzinfo=UTC)
UNKNOWN_ID = uuid.UUID("00000000-0000-7000-8000-000000000000")


async def _ready_run(db, *, reopen=True):
    """선택 대기 현재 실행 하나와 그 실행의 FEASIBLE 대응안·팀장"""
    row = (
        await db.execute(
            text(
                """
                SELECT r.company_id, r.analysis_run_id, r.delay_event_id,
                       (SELECT o.response_option_id FROM response_options o
                         WHERE o.analysis_run_id = r.analysis_run_id
                           AND o.feasibility_status = 'FEASIBLE'
                         ORDER BY o.option_type, o.response_option_id LIMIT 1) AS option_id,
                       (SELECT u.user_id FROM users u WHERE u.company_id = r.company_id
                         AND u.role_code = 'CLIENT_LEAD' ORDER BY u.user_id LIMIT 1) AS lead_id
                  FROM analysis_runs r
                 WHERE r.is_latest AND r.status = 'STALE'
                   AND r.completed_status = 'READY_FOR_REVIEW'
                   AND NOT EXISTS (SELECT 1 FROM final_decisions f
                                    WHERE f.analysis_run_id = r.analysis_run_id)
                 ORDER BY r.analysis_run_id
                 LIMIT 1
                """
            )
        )
    ).first()
    if row is None or row.option_id is None or row.lead_id is None:
        pytest.skip("선택 대기로 되돌릴 실행이 없는 DB")
    await db.execute(
        text(
            "UPDATE analysis_runs SET status = 'READY_FOR_REVIEW', stale_primary_reason = NULL,"
            " stale_reason_codes = NULL, stale_detected_seq = NULL, stale_detected_at = NULL"
            " WHERE analysis_run_id = :r"
        ),
        {"r": row.analysis_run_id},
    )
    if reopen:
        await db.execute(
            text(
                "UPDATE delay_events SET closed_at = NULL, close_reason = NULL"
                " WHERE delay_event_id = :e"
            ),
            {"e": row.delay_event_id},
        )
    return row


def _service(db) -> FinalDecisionService:
    return FinalDecisionService(
        db, FinalDecisionRepository(db), clock=DecisionClock.fixed(NOW)
    )


async def _decide(
    db,
    row,
    *,
    key="key-1",
    option_id=None,
    company_id=None,
    reason="재고 영향이 가장 적음",
):
    return await _service(db).decide(
        TenantContext(user_id=row.lead_id, company_id=company_id or row.company_id),
        analysis_run_id=row.analysis_run_id,
        response_option_id=option_id or row.option_id,
        reason_text=reason,
        idempotency_key=key,
    )


async def _run_state(db, run_id):
    return (
        await db.execute(
            text(
                "SELECT status, completed_status FROM analysis_runs WHERE analysis_run_id = :r"
            ),
            {"r": run_id},
        )
    ).one()


async def test_final_decision_saves_row_and_finalizes_run(db_write_session):
    db = db_write_session
    row = await _ready_run(db)
    max_seq = await db.scalar(
        text(
            "SELECT COALESCE(MAX(applied_seq), 0) FROM data_sync_runs WHERE company_id = :c"
        ),
        {"c": row.company_id},
    )

    result = await _decide(db, row)

    assert (result.status, result.decided_at, result.checked_seq, result.replayed) == (
        "FINALIZED",
        NOW,
        max_seq,
        False,
    )
    assert tuple(await _run_state(db, row.analysis_run_id)) == (
        "FINALIZED",
        "READY_FOR_REVIEW",
    )
    stored = (
        await db.execute(
            text(
                "SELECT final_decision_id, user_id, response_option_id, reason_text,"
                " idempotency_key, decided_at, checked_seq FROM final_decisions"
                " WHERE analysis_run_id = :r"
            ),
            {"r": row.analysis_run_id},
        )
    ).one()
    assert tuple(stored) == (
        result.final_decision_id,
        row.lead_id,
        row.option_id,
        "재고 영향이 가장 적음",
        "key-1",
        NOW,
        max_seq,
    )


async def test_same_key_returns_saved_result(db_write_session):
    db = db_write_session
    row = await _ready_run(db)
    first = await _decide(db, row)

    again = await _decide(db, row)

    assert (again.final_decision_id, again.replayed) == (first.final_decision_id, True)
    count = await db.scalar(
        text("SELECT count(*) FROM final_decisions WHERE analysis_run_id = :r"),
        {"r": row.analysis_run_id},
    )
    assert count == 1


async def test_other_key_after_final_decision_is_final_decision_exists(
    db_write_session,
):
    db = db_write_session
    row = await _ready_run(db)
    await _decide(db, row)

    with pytest.raises(DecisionException) as raised:
        await _decide(db, row, key="key-2")

    assert raised.value.error_code == DecisionErrorCode.FINAL_DECISION_EXISTS


async def test_key_used_for_other_run_is_rejected(db_write_session):
    db = db_write_session
    row = await _ready_run(db)
    used = await db.scalar(
        text(
            "SELECT idempotency_key FROM final_decisions WHERE company_id = :c"
            " ORDER BY final_decision_id LIMIT 1"
        ),
        {"c": row.company_id},
    )
    if used is None:
        pytest.skip("이 고객사에 최종 선택이 없는 DB")

    with pytest.raises(DecisionException) as raised:
        await _decide(db, row, key=used)

    assert raised.value.error_code == DecisionErrorCode.IDEMPOTENCY_KEY_REUSED
    assert (await _run_state(db, row.analysis_run_id)).status == "READY_FOR_REVIEW"


async def test_closed_episode_is_rejected_without_change(db_write_session):
    db = db_write_session
    row = await _ready_run(db, reopen=False)

    with pytest.raises(DecisionException) as raised:
        await _decide(db, row)

    assert raised.value.error_code == DecisionErrorCode.EPISODE_CLOSED
    assert (await _run_state(db, row.analysis_run_id)).status == "READY_FOR_REVIEW"


async def test_option_of_other_run_is_rejected(db_write_session):
    db = db_write_session
    row = await _ready_run(db)
    other_option = await db.scalar(
        text(
            "SELECT response_option_id FROM response_options WHERE analysis_run_id <> :r"
            " ORDER BY response_option_id LIMIT 1"
        ),
        {"r": row.analysis_run_id},
    )

    with pytest.raises(DecisionException) as raised:
        await _decide(db, row, option_id=other_option)

    assert raised.value.error_code == DecisionErrorCode.OPTION_NOT_IN_RUN


async def test_run_of_other_company_is_404(db_write_session):
    db = db_write_session
    row = await _ready_run(db)

    with pytest.raises(AnalysisRunNotFoundException):
        await _decide(db, row, company_id=UNKNOWN_ID)


async def test_same_key_with_other_reason_is_rejected(db_write_session):
    """같은 키·같은 실행이라도 선택 이유가 다르면 재요청으로 보지 않는다 (PR #46 리뷰)"""
    db = db_write_session
    row = await _ready_run(db)
    await _decide(db, row)

    with pytest.raises(DecisionException) as raised:
        await _decide(db, row, reason="다른 이유")

    assert raised.value.error_code == DecisionErrorCode.IDEMPOTENCY_KEY_REUSED
