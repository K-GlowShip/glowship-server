import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.core.security.tenant import TenantContext
from app.modules.data_sync.infrastructure.repository.data_sync_run_repository import (
    DataSyncRunRepository,
)
from app.modules.decision.application.decision_clock import DecisionClock
from app.modules.decision.application.exception.decision_exceptions import (
    DecisionErrorCode,
    DecisionException,
)
from app.modules.decision.application.reanalysis_service import ReanalysisService
from app.modules.decision.infrastructure.repository.member_draft_repository import (
    MemberDraftRepository,
)
from app.modules.delay_analysis.application.exception.analysis_run_query_exceptions import (
    AnalysisRunNotFoundException,
)

"""
    재분석 요청 서비스 SQL 테스트 (API 20, 실제 DB, 끝나면 롤백)
      - 확정 DB 는 지연이 모두 닫혀 있어 그대로면 EPISODE_CLOSED 다. 롤백 트랜잭션에서 에피소드의 closed_at 을 지워 연다.
      - 새 실행: version_no + 1, 부모 = 이전 현재 실행, USER_REANALYSIS·REANALYSIS, 요청자·요청 사유, DETECTED·is_latest.
        이전 실행: is_latest 만 false, 상태는 그대로(명세 4.6).
"""

UNKNOWN_ID = uuid.UUID("00000000-0000-7000-8000-000000000000")


async def _run(db, *, status: str, is_latest: bool):
    """조건에 맞는 실행과 그 고객사의 팀원 하나 (없으면 건너뜀)"""
    row = (
        await db.execute(
            text(
                """
                SELECT r.company_id, r.analysis_run_id, r.delay_event_id, r.version_no, r.status,
                       (SELECT u.user_id FROM users u
                         WHERE u.company_id = r.company_id AND u.role_code = 'CLIENT_MEMBER'
                         ORDER BY u.user_id LIMIT 1) AS member_id
                  FROM analysis_runs r
                 WHERE r.status = :s AND r.is_latest = :l
                 ORDER BY r.analysis_run_id
                 LIMIT 1
                """
            ),
            {"s": status, "l": is_latest},
        )
    ).first()
    if row is None or row.member_id is None:
        pytest.skip(f"{status}(is_latest={is_latest}) 실행이 없는 DB")
    return row


async def _reopen(db, delay_event_id):
    await db.execute(
        text(
            "UPDATE delay_events SET closed_at = NULL, close_reason = NULL"
            " WHERE delay_event_id = :e"
        ),
        {"e": delay_event_id},
    )


def _service(db) -> ReanalysisService:
    return ReanalysisService(db, MemberDraftRepository(db))


def _tenant(row) -> TenantContext:
    return TenantContext(user_id=row.member_id, company_id=row.company_id)


async def _stored(db, run_id):
    return (
        await db.execute(
            text(
                "SELECT status, is_latest, version_no, parent_analysis_run_id, trigger_type,"
                " trigger_reason, trigger_shipment_schedule_snapshot_id, requested_by_user_id,"
                " request_reason, requested_at, run_code"
                " FROM analysis_runs WHERE analysis_run_id = :r"
            ),
            {"r": run_id},
        )
    ).one()


@pytest.mark.parametrize("status", ["STALE", "FAILED", "NO_RISK"])
async def test_reanalysis_creates_next_version_and_keeps_previous_status(
    db_write_session, status
):
    db = db_write_session
    row = await _run(db, status=status, is_latest=True)
    await _reopen(db, row.delay_event_id)

    result = await _service(db).request(
        _tenant(row),
        analysis_run_id=row.analysis_run_id,
        request_reason="입고일 재확인",
    )

    assert (result.version_no, result.parent_run_id, result.status) == (
        row.version_no + 1,
        row.analysis_run_id,
        "DETECTED",
    )
    new = await _stored(db, result.run_id)
    assert (new.status, new.is_latest, new.version_no) == (
        "DETECTED",
        True,
        row.version_no + 1,
    )
    assert (new.trigger_type, new.trigger_reason) == ("REANALYSIS", "USER_REANALYSIS")
    assert new.trigger_shipment_schedule_snapshot_id is None  # ck16
    assert (new.requested_by_user_id, new.request_reason) == (
        row.member_id,
        "입고일 재확인",
    )
    assert new.requested_at is not None
    assert new.run_code.startswith("AN-")
    previous = await _stored(db, row.analysis_run_id)
    # 재분석은 STALE 사유가 아니다: 상태 그대로, is_latest 만 내려감
    assert (previous.status, previous.is_latest) == (status, False)


_NEWER_RULE_SET = text(
    """
    INSERT INTO operating_rule_sets (operating_rule_set_id, company_id, version_no, user_id, effective_from,
           safety_stock_days, sales_basis_days, allow_partial_fulfillment, created_at)
    SELECT :id, company_id, version_no + 1, user_id, :effective_from,
           safety_stock_days, sales_basis_days, allow_partial_fulfillment, now()
      FROM operating_rule_sets
     WHERE company_id = :c
     ORDER BY version_no DESC
     LIMIT 1
    """
)


async def test_virtual_clock_reanalysis_runs_at_last_applied_sync_start(
    db_write_session,
):
    """
    이슈 #50: 가상 시각이면 재분석 실행 시각 = 고객사 마지막 적재 수집 시작(7월 초, L1 뒤).
    기준일·run_code·requested_at 이 그 시각을 따르고, 그 뒤(실제 날짜 전)에 시작하는 운영 기준 버전이 있어도
    실행은 7월 초에 적용 중인 버전을 고른다(최종 선택의 "선택일에 적용 중인 버전"과 같은 기준일).
    """
    db = db_write_session
    row = await _run(db, status="STALE", is_latest=True)
    await _reopen(db, row.delay_event_id)
    last_input = await DataSyncRunRepository(
        db
    ).find_last_applied_started_at_of_company(row.company_id)
    newer_rule = uuid.uuid7()
    await db.execute(
        _NEWER_RULE_SET,
        {
            "id": newer_rule,
            "c": row.company_id,
            "effective_from": last_input.date() + timedelta(days=1),
        },
    )
    service = ReanalysisService(
        db,
        MemberDraftRepository(db),
        DecisionClock(DataSyncRunRepository(db), virtual=True),
    )

    result = await service.request(
        _tenant(row), analysis_run_id=row.analysis_run_id, request_reason=None
    )

    new = (
        await db.execute(
            text(
                "SELECT started_at, requested_at, run_code, operating_rule_set_id"
                " FROM analysis_runs WHERE analysis_run_id = :r"
            ),
            {"r": result.run_id},
        )
    ).one()
    assert (new.started_at, new.requested_at) == (last_input, last_input)
    assert new.run_code.startswith(f"AN-{last_input:%y%m%d}-")
    assert new.operating_rule_set_id != newer_rule


async def test_closed_episode_is_rejected_without_change(db_write_session):
    db = db_write_session
    row = await _run(db, status="STALE", is_latest=True)

    with pytest.raises(DecisionException) as raised:
        await _service(db).request(
            _tenant(row), analysis_run_id=row.analysis_run_id, request_reason=None
        )

    assert raised.value.error_code == DecisionErrorCode.EPISODE_CLOSED
    assert (await _stored(db, row.analysis_run_id)).is_latest is True


async def test_previous_version_is_not_current_run(db_write_session):
    db = db_write_session
    row = await _run(db, status="STALE", is_latest=False)
    await _reopen(db, row.delay_event_id)

    with pytest.raises(DecisionException) as raised:
        await _service(db).request(
            _tenant(row), analysis_run_id=row.analysis_run_id, request_reason=None
        )

    assert raised.value.error_code == DecisionErrorCode.NOT_CURRENT_RUN


async def test_no_delay_run_is_not_allowed(db_write_session):
    db = db_write_session
    row = await _run(db, status="NO_DELAY", is_latest=True)
    await _reopen(db, row.delay_event_id)

    with pytest.raises(DecisionException) as raised:
        await _service(db).request(
            _tenant(row), analysis_run_id=row.analysis_run_id, request_reason=None
        )

    assert raised.value.error_code == DecisionErrorCode.REANALYSIS_NOT_ALLOWED
    count = await db.scalar(
        text("SELECT count(*) FROM analysis_runs WHERE delay_event_id = :e"),
        {"e": row.delay_event_id},
    )
    assert count == row.version_no


async def test_run_of_other_company_or_unknown_is_404(db_write_session):
    db = db_write_session
    row = await _run(db, status="STALE", is_latest=True)
    other = TenantContext(user_id=row.member_id, company_id=UNKNOWN_ID)

    for tenant, run_id in (
        (other, row.analysis_run_id),
        (_tenant(row), UNKNOWN_ID),
    ):
        with pytest.raises(AnalysisRunNotFoundException):
            await _service(db).request(
                tenant, analysis_run_id=run_id, request_reason=None
            )
