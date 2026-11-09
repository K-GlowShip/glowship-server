"""
GS-04 주문 배정: 우선순위·원자성·건너뛰기 + b-2 (02_시나리오와정답.md 5절 GS-04)
  - 분석 실행이 아니라 D−1 하루 배정 시험이라 T9·T11·①~⑤는 해당 없음. 엔진 배정기(allocate)를 fixture 로 돌린다.
  - 기대: 우선순위 O-A, O-B, O-C, O-D, O-H, O-I. 03-10 확보 {O-A, O-C, O-H}, 03-11 출고 같음.
    대기 O-B(X 6 DIRECT, Y 3 BUNDLED), O-D(X 0 DIRECT), O-I(Z 0 DIRECT).
    보유 03-10 10/5/3 → 03-11 6/3/0, 예약 같음, 가용 0, 확보일 03-10·출고일 03-11.
  - 대기 원인 이름은 정답표가 DIRECT·BUNDLED 로만 적어서 앞부분만 견준다.
"""

EXPECTED = {
    "secured_0310": ["O-A", "O-C", "O-H"],
    "dispatched_0311": ["O-A", "O-C", "O-H"],
    "waiting": ["O-B", "O-D", "O-I"],
    "reserved_0310": {"X": 10, "Y": 5, "Z": 3},
    "on_hand_0311": {"X": 6, "Y": 3, "Z": 0},
    "reserved_0311": {"X": 6, "Y": 3, "Z": 0},
    "available": {"X": 0, "Y": 0, "Z": 0},
    "dispatch_date": "2026-03-11",
    "invariants": True,
}
PROTECTION = {
    "O-B": {"X": (6, "DIRECT"), "Y": (3, "BUNDLED")},
    "O-D": {"X": (0, "DIRECT")},
    "O-I": {"Z": (0, "DIRECT")},
}


def test_allocation_matches_answer_sheet(glow50):
    got = glow50.gs04()
    assert {k: got[k] for k in EXPECTED} == EXPECTED
    assert set(got["protection"]) == set(PROTECTION)
    for order, skus in PROTECTION.items():
        assert set(got["protection"][order]) == set(skus), order
        for sku, (quantity, cause_prefix) in skus.items():
            protected, cause = got["protection"][order][sku]
            assert protected == quantity, (order, sku)
            assert cause.startswith(cause_prefix), (order, sku, cause)
