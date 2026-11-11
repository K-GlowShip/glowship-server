from datetime import UTC, datetime

import pytest
from real_path_support import (
    answer_runs,
    fix_explanation_canary,
    require_engine,
    rerun,
)
from sqlalchemy import text

from app.modules.delay_analysis.application.stale.stale_service import (
    StaleJudgmentService,
)
from app.modules.delay_analysis.infrastructure.repository.stale_change_repository import (
    StaleChangeRepository,
)

"""
    서버 STALE 판정을 확정 데이터의 STALE 열과 대조 (GLOW-58 2단계 G 갈래)
      - 확정 데이터의 STALE 열(주 사유·감지 순번)은 엔진 하네스가 판정 서명을 다시 계산해 "서명이 바뀐 수집"에서 남긴 값이다
        (serviceflow 5.2 "판정 서명이 달라질 때만 STALE", 명세 D23).
      - 서버 판정(StaleJudgmentService)에는 아직 판정 서명 재계산기가 연결돼 있지 않다. 재계산기가 없으면 의존 범위에
        걸린 첫 수집에서 STALE 로 본다(놓치지 않는 쪽, stale_service._signature_changed). 재계산기 연결은 이슈 #75 다.
      - 그래서 완료 실행 17건 가운데 9건은 정답과 같고 8건은 다르다. 같은 9건은 통과로 고정하고, 다른 8건은
        "정답과 같아야 한다"를 strict xfail 로 두고 지금 값(걸린 첫 수집)도 따로 고정한다.
      - 방법: 롤백 세션에서 실행을 "완료 직후·열린 에피소드·그때의 현재 버전"으로 되돌리고 recheck_run 을 부른다.
        상한(up_to_seq)은 그 실행이 살아 있던 구간이다: 정답이 관련 변경(RELEVANT_*)으로 STALE 이면 그 순번까지,
        아니면 다음 버전이 생기기 전과 에피소드가 닫히기 전 중 이른 쪽까지.
      - 이 시험은 서비스를 server_stale_service 한 곳에서 재계산기 없이 만든다. #75 가 머지되면 그 한 줄에 재계산기를
        넘겨 다시 돌리고 xfail 을 지운다. 그때를 알려 주는 것이 연결 감지 시험이다: 현재 버전 실행 1건을 실제 경로
        (실행기 → save_graph_outcome → 저장 직후 재판정)로 돌려, 앱의 저장 경로에 재계산기가 연결되면 XPASS 가 된다.
        수집 쪽 연결(judge_after_sync)은 이 시험이 감지하지 못한다.
"""

NOW = datetime(2026, 7, 1, tzinfo=UTC)
ORDER = "RELEVANT_ORDER_CHANGED"
A, B = "A_BEAUTY", "B_COSMETICS"

# 정답과 같은 9건: (고객사 코드, run_code) → 정답 STALE (주 사유, 감지 순번) 또는 None(STALE 아님)
SAME = {
    (A, "AN-260101-001"): (ORDER, 13),
    (A, "AN-260101-002"): None,  # 관련 변경 전에 최종 선택이 끝남
    (A, "AN-260101-003"): (ORDER, 13),
    (A, "AN-260101-004"): (ORDER, 13),
    (A, "AN-260102-001"): None,  # 관련 변경 전에 최종 선택이 끝남
    (A, "AN-260103-001"): (ORDER, 23),
    (A, "AN-260118-001"): (ORDER, 98),
    (A, "AN-260119-001"): (ORDER, 103),
    (B, "AN-260105-001"): (ORDER, 33),
}
# 정답과 다른 8건: → (정답, 재계산기 없는 서버 판정)
DIFFERENT = {
    # 정답보다 일찍 STALE (같은 사유, 앞선 수집)
    (A, "AN-260115-001"): ((ORDER, 143), (ORDER, 83)),
    (A, "AN-260115-002"): ((ORDER, 88), (ORDER, 83)),
    (A, "AN-260202-001"): ((ORDER, 178), (ORDER, 173)),
    (B, "AN-260103-002"): ((ORDER, 28), (ORDER, 23)),
    # 정답은 일정·단계 변경(후속 실행 생성)으로 STALE, 서버는 그 전에 주문 변경으로 STALE
    (B, "AN-260101-001"): (("DELAY_SCHEDULE_CHANGED", 15), (ORDER, 13)),
    (B, "AN-260103-001"): (("DELAY_SCHEDULE_CHANGED", 30), (ORDER, 23)),
    (B, "AN-260115-001"): (("DELAY_STAGE_CHANGED", 90), (ORDER, 83)),
    # 정답은 끝까지 STALE 이 아님
    (B, "AN-260117-001"): (None, (ORDER, 93)),
}
NO_RECALCULATOR = "서버에 판정 서명 재계산기가 연결돼 있지 않음(이슈 #75)"
CONNECTION_PROBE = (A, "AN-260202-001")  # 현재 버전, 정답 STALE 순번 178

_COMPLETED = text(
    """
    SELECT c.company_code, r.run_code, r.analysis_run_id, r.delay_event_id, r.input_seq, r.status,
           r.stale_primary_reason, r.stale_detected_seq,
           (SELECT min(n.input_seq) FROM analysis_runs n
             WHERE n.delay_event_id = r.delay_event_id AND n.version_no > r.version_no) AS next_version_seq,
           (SELECT max(s.applied_seq) FROM data_sync_runs s
             WHERE s.company_id = r.company_id AND s.applied_seq IS NOT NULL
               AND s.finished_at <= e.closed_at) AS close_seq,
           (SELECT f.checked_seq FROM final_decisions f
             WHERE f.analysis_run_id = r.analysis_run_id) AS decided_seq
      FROM analysis_runs r
      JOIN companies c USING (company_id)
      JOIN delay_events e USING (delay_event_id)
     WHERE r.completed_status IN ('READY_FOR_REVIEW', 'NO_RISK')
     ORDER BY c.company_code, r.run_code
    """
)
_LOWER_LATER_VERSIONS = text(
    "UPDATE analysis_runs SET is_latest = false"
    " WHERE delay_event_id = :event AND analysis_run_id <> :id AND is_latest"
)
_BACK_TO_COMPLETED = text(
    "UPDATE analysis_runs SET is_latest = true,"
    " status = CASE WHEN status = 'STALE' THEN completed_status ELSE status END,"
    " stale_primary_reason = NULL, stale_reason_codes = NULL,"
    " stale_detected_seq = NULL, stale_detected_at = NULL WHERE analysis_run_id = :id"
)
_REOPEN = text(
    "UPDATE delay_events SET closed_at = NULL, close_reason = NULL"
    " WHERE delay_event_id = :event"
)

_probe: dict = {}  # 연결 감지 실행의 저장 결과(시험 둘이 한 번만 돌린다)


def server_stale_service(db) -> StaleJudgmentService:
    """앱이 만드는 모양 그대로(재계산기 없음). 이슈 #75 가 머지되면 여기에 재계산기를 넘겨 17건을 다시 돌린다."""
    return StaleJudgmentService(db, StaleChangeRepository(db))


async def _completed(db) -> dict:
    rows = (await db.execute(_COMPLETED)).all()
    if not rows:
        pytest.skip("완료 실행이 없는 DB 라 건너뜀")
    return {(r.company_code, r.run_code): r for r in rows}


def _answer(row):
    if row.stale_primary_reason is None:
        return None
    return (row.stale_primary_reason, row.stale_detected_seq)


def _alive_until(row) -> int:
    """그 실행이 살아 있던 구간의 끝(적용 순번)."""
    if row.stale_primary_reason is not None and row.stale_primary_reason.startswith(
        "RELEVANT_"
    ):
        return row.stale_detected_seq
    return min(seq for seq in (row.next_version_seq, row.close_seq) if seq is not None)


async def _server_judgment(db, row):
    """실행을 완료 직후·열린 에피소드·그때의 현재 버전으로 되돌려 서버 판정을 돌린 값. 끝나면 되돌린다."""
    savepoint = await db.begin_nested()
    try:
        ids = {"id": row.analysis_run_id, "event": row.delay_event_id}
        await db.execute(_LOWER_LATER_VERSIONS, ids)
        await db.execute(_BACK_TO_COMPLETED, ids)
        await db.execute(_REOPEN, ids)
        db.expire_all()
        staled = await server_stale_service(db).recheck_run(
            row.analysis_run_id, NOW, up_to_seq=_alive_until(row)
        )
        if staled is None:
            return None
        return (staled.stale_primary_reason, staled.stale_detected_seq)
    finally:
        await savepoint.rollback()
        db.expire_all()


async def test_answer_table_matches_confirmed_data(db_write_session):
    """완료 실행 17건 = 같음 9 + 다름 8, 표의 정답 값이 확정 데이터와 같다(데이터가 바뀌면 조용히 줄지 않게)."""
    rows = await _completed(db_write_session)

    assert len(SAME) == 9 and len(DIFFERENT) == 8
    assert set(rows) == set(SAME) | set(DIFFERENT) and len(rows) == 17
    assert {key: _answer(rows[key]) for key in SAME} == SAME
    assert {key: _answer(rows[key]) for key in DIFFERENT} == {
        key: answer for key, (answer, _server) in DIFFERENT.items()
    }
    # 최종 선택이 끝난 2건은 선택 때까지 새 수집이 없었다(관련 변경 전에 끝남)
    decided = {key: row for key, row in rows.items() if row.status == "FINALIZED"}
    assert set(decided) == {key for key, answer in SAME.items() if answer is None}
    assert all(row.decided_seq == row.input_seq for row in decided.values())


@pytest.mark.parametrize("key", list(SAME), ids="/".join)
async def test_server_stale_judgment_matches_answer(db_write_session, key):
    """정답과 같은 9건: STALE 여부·주 사유·감지 순번이 확정 데이터와 같다."""
    db = db_write_session
    rows = await _completed(db)

    assert await _server_judgment(db, rows[key]) == SAME[key]


@pytest.mark.parametrize("key", list(DIFFERENT), ids="/".join)
@pytest.mark.xfail(strict=True, reason=NO_RECALCULATOR)
async def test_server_stale_judgment_should_match_answer(db_write_session, key):
    """정답과 다른 8건: 재계산기가 연결되면(server_stale_service 를 고친 뒤) 정답과 같아져야 한다."""
    db = db_write_session
    rows = await _completed(db)

    assert await _server_judgment(db, rows[key]) == DIFFERENT[key][0]


@pytest.mark.parametrize("key", list(DIFFERENT), ids="/".join)
async def test_server_without_recalculator_stales_at_first_relevant_sync(
    db_write_session, key
):
    """같은 8건의 지금 값: 재계산기가 없으면 의존 범위에 걸린 첫 수집에서 STALE 로 바꾼다."""
    db = db_write_session
    rows = await _completed(db)

    assert await _server_judgment(db, rows[key]) == DIFFERENT[key][1]


async def _saved_through_real_path(db, monkeypatch) -> dict:
    """연결 감지 실행을 에피소드를 다시 연 채 실제 경로로 저장한 뒤의 STALE 열."""
    if not _probe:
        require_engine()
        runs = await answer_runs(db)
        if CONNECTION_PROBE not in runs:
            pytest.skip("정답 overlay 실행이 없는 DB 라 건너뜀")
        run = runs[CONNECTION_PROBE]
        fix_explanation_canary(monkeypatch)
        await db.execute(_REOPEN, {"event": run.delay_event_id})
        result = await rerun(db, run)
        row = result.after["run"]
        _probe.update(
            status=row["status"],
            completed_status=row["completed_status"],
            stale=(row["stale_primary_reason"], row["stale_detected_seq"]),
            impacts=len(result.after["inventory"]),
        )
    return _probe


@pytest.mark.xfail(strict=True, reason=NO_RECALCULATOR)
async def test_saved_result_stale_seq_should_match_answer(
    db_write_session, monkeypatch
):
    """
    연결 감지: 앱의 저장 경로(save_graph_outcome → 저장 직후 재판정)에 재계산기가 연결되면 정답 순번이 되어 XPASS 가 된다.
    그때 server_stale_service 에 재계산기를 넘기고, 위 8건의 xfail 과 이 표시를 지운다.
    """
    saved = await _saved_through_real_path(db_write_session, monkeypatch)

    assert saved["stale"] == DIFFERENT[CONNECTION_PROBE][0]


async def test_saved_result_is_stale_at_first_relevant_sync(
    db_write_session, monkeypatch
):
    """연결 감지 실행의 지금 값: 저장 직후 재판정이 걸린 첫 수집에서 STALE 로 바꾸고 결과는 남긴다."""
    saved = await _saved_through_real_path(db_write_session, monkeypatch)

    assert saved["stale"] == DIFFERENT[CONNECTION_PROBE][1]
    assert (saved["status"], saved["completed_status"]) == ("STALE", "READY_FOR_REVIEW")
    assert saved["impacts"] > 0
