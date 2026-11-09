import pytest

"""
    GS-09 일반 데이터 변경 → 서명 재계산 → STALE 여부 (02_시나리오와정답.md 5절 GS-09)
      - R1 = GS-03. 변경 A(늦게 도착한 O2, 03-11 수집) → RELEVANT_ORDER_CHANGED, 서명 다름 → STALE.
        변경 B(재고 50 → 45) → RELEVANT_INVENTORY_CHANGED, 서명 같음 → READY_FOR_REVIEW 유지.
      - 재계산 값(A: 111·111, 222, 110, O1·O2 DELAY_INDUCED, KR 회복 2)은 test_evaluation_t9_t11.py GS-09A.
      - 엔진 1차 필터(stale.first_filter)와 서명 비교까지만 본다. 서버 StaleJudgmentService 대조는 2단계(DB 필요).
"""

EXPECTED = {
    "A": {
        "first_filter": ["RELEVANT_ORDER_CHANGED"],
        "signature_changed": True,
        "r1_status": "STALE",
        "stale_primary_reason": "RELEVANT_ORDER_CHANGED",
        "replay_reserved": {"CREAM20": 50},
    },
    "B": {
        "first_filter": ["RELEVANT_INVENTORY_CHANGED"],
        "signature_changed": False,
        "r1_status": "READY_FOR_REVIEW",
        "stale_primary_reason": None,
        "replay_reserved": {"CREAM20": 45},
    },
}


@pytest.fixture(scope="module")
def gs09(glow50):
    snap1, r1, changes = glow50.gs09()
    return snap1, r1, changes


def test_r1_is_gs03(gs09, scenario):
    _snap1, r1, _changes = gs09
    _snap, _replay, gs03 = scenario("GS-03")
    assert r1["sha"] == gs03["sha"]


@pytest.mark.parametrize("change", ["A", "B"])
def test_change_judgement_matches_answer_sheet(gs09, change):
    _snap1, r1, changes = gs09
    got = changes[change]
    assert {k: got[k] for k in EXPECTED[change]} == EXPECTED[change]
    assert (got["new_sha"] != r1["sha"]) == EXPECTED[change]["signature_changed"]
