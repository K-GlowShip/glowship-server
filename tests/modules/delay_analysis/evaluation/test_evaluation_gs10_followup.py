"""
GS-10 지연 선적 일정 변경 → 후속 AUTO 실행 (02_시나리오와정답.md 5절 GS-10)
  - 같은 선적의 03-10 관측(E 03-30)·03-11 관측(E 04-06) → 트리거 03-10 EPISODE_STARTED, 03-11 SCHEDULE_WORSENED.
  - 새 실행: AUTO / SCHEDULE_WORSENED, 부모 R1, D' 03-11, E' 04-06. R1 STALE(DELAY_SCHEDULE_CHANGED).
  - D'−1 재생 예약 50(O3 10 확보 + O1 보호 40). 새 실행의 ①~⑤·T9·T11 은 test_evaluation_t9_t11.py GS-10.
"""

EXPECTED = {
    "triggers": [
        ["2026-03-10", "EPISODE_STARTED"],
        ["2026-03-11", "SCHEDULE_WORSENED"],
    ],
    "trigger_type": "AUTO",
    "trigger_reason": "SCHEDULE_WORSENED",
    "parent": "R1",
    "D": "2026-03-11",
    "E": "2026-04-06",
    "r1_status": "STALE",
    "r1_stale_reason": "DELAY_SCHEDULE_CHANGED",
    "replay_reserved": {"CREAM20": 50},
}


def test_follow_up_run_matches_answer_sheet(glow50, scenario):
    snap1, r1, snap2, r2, got = glow50.gs10()
    assert {k: got[k] for k in EXPECTED} == EXPECTED
    _snap, _replay, gs03 = scenario("GS-03")
    assert r1["sha"] == gs03["sha"]  # R1 = GS-03
    assert (
        snap1["E"].isoformat() == "2026-03-30"
        and snap2["E"].isoformat() == "2026-04-06"
    )
    assert r2["sha"] != r1["sha"]
