import os
from datetime import UTC, datetime

import pytest
from real_path_support import (
    answer_runs,
    fix_explanation_canary,
    require_engine,
    rerun,
)
from sqlalchemy import text

from app.modules.delay_analysis.application.run_creation.auto_run_service import (
    previous_run_stale_reason,
)
from app.modules.delay_analysis.application.stale.stale_service import (
    StaleJudgmentService,
)
from app.modules.delay_analysis.infrastructure.analysis_enums import TriggerReason
from app.modules.delay_analysis.infrastructure.engine.signature_recalculator import (
    recalculator_or_none,
)
from app.modules.delay_analysis.infrastructure.repository.stale_change_repository import (
    StaleChangeRepository,
)

"""
    서버 STALE 판정을 확정 데이터의 STALE 열과 대조 (GLOW-58 2단계 G 갈래)
      - 확정 데이터의 STALE 열(주 사유·감지 순번)은 엔진 하네스가 판정 서명을 다시 계산해 "서명이 바뀐 수집"에서 남긴 값이다
        (serviceflow 5.2 "판정 서명이 달라질 때만 STALE", 명세 D23). 서버도 #116(이슈 #75)부터 판정 서명 재계산기로 같은 일을 한다.
      - 완료 실행 17건을 두 갈래로 나눈다.
          · RECHECK 14건: 저장 직후 재판정(recheck_run)이 정답과 같은 STALE 여부·주 사유·감지 순번을 낸다.
          · FOLLOW_UP 3건: 정답 STALE 은 다음 버전 실행을 만들 때 생긴다(일정·단계 변경, auto_run_service 가 mark_runs_stale).
            재판정만으로는 나올 수 없으므로, 다음 버전 직전까지 재판정은 STALE 이 아니고(서명이 같음) 확정 데이터의 다음 버전
            순번·트리거 사유가 정답 순번·사유와 맞는지를 본다. 후속 실행을 만들며 이전 실행을 STALE 로 바꾸는 서버 동작은
            수집 → 저장 한 흐름 시험(test_evaluation_runner_gaps B5)이 본다.
      - 방법: 롤백 세션에서 실행을 "완료 직후·열린 에피소드·그때의 현재 버전"으로 되돌리고 recheck_run 을 부른다.
        상한(up_to_seq)은 그 실행이 살아 있던 구간이다: 정답이 관련 변경(RELEVANT_*)으로 STALE 이면 그 순번까지,
        아니면 다음 버전이 생길 때와 에피소드가 닫힐 때 중 이른 쪽까지.
      - 재계산 한 번이 새 순번 저장소를 캐시 없이 읽어 이 PC 에서 6~26초다(17건에 재계산 37번, 이 파일을 정답 전체 켬으로
        돌리면 약 6분, 11-11). 그래서 17건 전체는 GLOWSHIP_ENGINE_GOLDEN=1 일 때만 돌고, 평소에는 짧은 대표 1건
        (FOLLOW_UP)과 아래 연결 확인만 돈다.
      - 대조는 시험 접속 계정으로 돈다(워커 역할·RLS 문맥 없음). 워커 역할로 도는 것은 아래 연결 확인(실제 경로)이다.
      - 재계산기는 앱(수집·결과 저장)과 같이 판정 서비스 하나에 하나를 만든다(recalculator_or_none, server_stale_service).
        재계산기는 마지막으로 읽은 순번 저장소 하나만 든다(#119). 그 전에는 읽은 순번을 모두 들어, 관련 순번이 14개인
        실행(A AN-260115-001)에서 저장소 14개가 함께 살았다(docs/analysis/evaluation-scenarios.md 8.5·8.6절).
      - 연결 확인: 현재 버전 실행 1건(A AN-260202-001)을 실제 경로(실행기 → save_graph_outcome → 저장 직후 재판정)로 돌려,
        앱의 저장 경로가 재계산기를 넘겨 정답 순번(178, 재계산기가 없으면 173)이 되는지 본다.
"""

NOW = datetime(2026, 7, 1, tzinfo=UTC)
ORDER = "RELEVANT_ORDER_CHANGED"
A, B = "A_BEAUTY", "B_COSMETICS"

# 저장 직후 재판정이 정답과 같은 14건: (고객사 코드, run_code) → 정답 STALE (주 사유, 감지 순번) 또는 None(STALE 아님)
RECHECK = {
    (A, "AN-260101-001"): (ORDER, 13),
    (A, "AN-260101-002"): None,  # 관련 변경 전에 최종 선택이 끝남
    (A, "AN-260101-003"): (ORDER, 13),
    (A, "AN-260101-004"): (ORDER, 13),
    (A, "AN-260102-001"): None,  # 관련 변경 전에 최종 선택이 끝남
    (A, "AN-260103-001"): (ORDER, 23),
    (A, "AN-260115-001"): (ORDER, 143),  # 83~139 의 관련 변경 13번은 서명이 같음
    (A, "AN-260115-002"): (ORDER, 88),  # 83 은 서명이 같음
    (A, "AN-260118-001"): (ORDER, 98),
    (A, "AN-260119-001"): (ORDER, 103),
    (A, "AN-260202-001"): (ORDER, 178),  # 173 은 서명이 같음
    (B, "AN-260103-002"): (ORDER, 28),  # 23 은 서명이 같음
    (B, "AN-260105-001"): (ORDER, 33),
    (B, "AN-260117-001"): None,  # 93~113 의 관련 변경 5번 모두 서명이 같음
}
# 다음 버전 실행이 만들어질 때 STALE 이 된 3건: → 정답 STALE (주 사유, 감지 순번). 그 직전까지 재판정은 STALE 아님
FOLLOW_UP = {
    (B, "AN-260101-001"): ("DELAY_SCHEDULE_CHANGED", 15),  # 13 은 서명이 같음
    (B, "AN-260103-001"): ("DELAY_SCHEDULE_CHANGED", 30),  # 23·28 은 서명이 같음
    (B, "AN-260115-001"): ("DELAY_STAGE_CHANGED", 90),  # 83·88 은 서명이 같음
}
REPRESENTATIVE = {(B, "AN-260101-001")}  # 평소에도 도는 건(재계산 1번, 이 PC 약 11초)
CONNECTION_PROBE = (A, "AN-260202-001")  # 현재 버전, 정답 STALE 순번 178

_COMPLETED = text(
    """
    SELECT c.company_code, r.run_code, r.analysis_run_id, r.delay_event_id, r.input_seq, r.status,
           r.stale_primary_reason, r.stale_detected_seq,
           (SELECT min(n.input_seq) FROM analysis_runs n
             WHERE n.delay_event_id = r.delay_event_id AND n.version_no > r.version_no) AS next_version_seq,
           (SELECT n.trigger_reason FROM analysis_runs n
             WHERE n.delay_event_id = r.delay_event_id AND n.version_no = r.version_no + 1) AS next_version_reason,
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


def server_stale_service(db) -> StaleJudgmentService:
    """앱(수집·결과 저장)이 만드는 모양 그대로. 엔진 경로가 없으면 건너뛴다(재계산기 없이는 1차만이라 정답과 다름)."""
    require_engine()
    return StaleJudgmentService(db, StaleChangeRepository(db), recalculator_or_none(db))


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


def _golden() -> bool:
    return os.environ.get("GLOWSHIP_ENGINE_GOLDEN") == "1"


def _needs_golden(key) -> None:
    if key not in REPRESENTATIVE and not _golden():
        pytest.skip("재계산이 몇 초~몇 분이라 GLOWSHIP_ENGINE_GOLDEN=1 일 때만 도는 건")


async def test_answer_table_matches_confirmed_data(db_write_session):
    """완료 실행 17건 = 재판정 14 + 후속 3, 표의 정답 값이 확정 데이터와 같다(데이터가 바뀌면 조용히 줄지 않게)."""
    rows = await _completed(db_write_session)

    assert len(RECHECK) == 14 and len(FOLLOW_UP) == 3
    assert set(rows) == set(RECHECK) | set(FOLLOW_UP) and len(rows) == 17
    assert {key: _answer(rows[key]) for key in RECHECK} == RECHECK
    assert {key: _answer(rows[key]) for key in FOLLOW_UP} == FOLLOW_UP
    # 최종 선택이 끝난 2건은 선택 때까지 새 수집이 없었다(관련 변경 전에 끝남)
    decided = {key: row for key, row in rows.items() if row.status == "FINALIZED"}
    assert set(decided) == {(A, "AN-260101-002"), (A, "AN-260102-001")}
    assert all(row.decided_seq == row.input_seq for row in decided.values())


@pytest.mark.parametrize("key", list(RECHECK), ids="/".join)
async def test_server_recheck_matches_answer(db_write_session, key):
    """재판정 14건: STALE 여부·주 사유·감지 순번이 확정 데이터와 같다(판정 서명이 바뀐 수집에서만 STALE)."""
    _needs_golden(key)
    db = db_write_session
    rows = await _completed(db)

    assert await _server_judgment(db, rows[key]) == RECHECK[key]


@pytest.mark.parametrize("key", list(FOLLOW_UP), ids="/".join)
async def test_follow_up_runs_stale_only_at_next_version(db_write_session, key):
    """후속 3건: 다음 버전 직전까지 재판정은 STALE 아님, 정답 순번·사유는 다음 버전 실행의 입력 순번·트리거 사유."""
    _needs_golden(key)
    db = db_write_session
    row = (await _completed(db))[key]
    reason, seq = FOLLOW_UP[key]

    assert await _server_judgment(db, row) is None
    assert row.next_version_seq == seq
    assert previous_run_stale_reason(TriggerReason(row.next_version_reason)) == reason


async def _saved_through_real_path(db, monkeypatch) -> dict:
    """연결 확인 실행을 에피소드를 다시 연 채 실제 경로로 저장한 뒤의 STALE 열."""
    require_engine()
    runs = await answer_runs(db)
    if CONNECTION_PROBE not in runs:
        pytest.skip("정답 overlay 실행이 없는 DB 라 건너뜀")
    run = runs[CONNECTION_PROBE]
    fix_explanation_canary(monkeypatch)
    await db.execute(_REOPEN, {"event": run.delay_event_id})
    result = await rerun(db, run)
    row = result.after["run"]
    return {
        "status": row["status"],
        "completed_status": row["completed_status"],
        "stale": (row["stale_primary_reason"], row["stale_detected_seq"]),
        "impacts": len(result.after["inventory"]),
    }


async def test_saved_result_goes_stale_where_signature_changes(
    db_write_session, monkeypatch
):
    """연결 확인: 앱의 저장 경로(save_graph_outcome → 저장 직후 재판정)가 재계산기를 넘겨 정답 순번에서 STALE, 결과는 남긴다."""
    saved = await _saved_through_real_path(db_write_session, monkeypatch)

    assert saved["stale"] == RECHECK[CONNECTION_PROBE]  # 재계산기가 없으면 173
    assert (saved["status"], saved["completed_status"]) == ("STALE", "READY_FOR_REVIEW")
    assert saved["impacts"] > 0
