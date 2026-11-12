"""
GS-04 주문 배정: 우선순위·원자성·건너뛰기 + b-2 (02_시나리오와정답.md 5절 GS-04)
  - 분석 실행이 아니라 D−1 하루 배정 시험이라 T9·T11·①~⑤는 해당 없음. 엔진 배정기(allocate)를 fixture 로 돌린다.
  - 기대: 우선순위 O-A, O-B, O-C, O-D, O-H, O-I. 03-10 확보 {O-A, O-C, O-H}, 03-11 출고 같음.
    대기 O-B(X 6 DIRECT, Y 3 BUNDLED), O-D(X 0 DIRECT), O-I(Z 0 DIRECT).
    보유 03-10 10/5/3 → 03-11 6/3/0, 예약 같음, 가용 0, 확보일 03-10·출고일 03-11.
  - 대기 원인 이름은 정답표가 DIRECT·BUNDLED 로만 적어서 앞부분만 견준다.
  - 03-10 보유(정답표 v6 3절 "보유·예약 03-10 10/5/3")는 fixture 의 gs04() 가 돌려주지 않는다. 엔진 파일은 바꾸지
    않고, gs04() 가 부르는 배정기(simulate)의 하루치 결과를 받아 03-10 마감 보유를 본다.
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


def test_on_hand_0310_matches_answer_sheet(glow50, monkeypatch):
    """03-10 은 확보만 하고 출고는 03-11 이라 03-10 마감 보유는 시작과 같은 10/5/3 이다(출고를 03-10 에 빼면 깨짐)."""
    days = []
    simulate = glow50.AC.simulate

    def keep_days(*args, **kwargs):
        days.append(simulate(*args, **kwargs))
        return days[-1]

    monkeypatch.setattr(glow50.AC, "simulate", keep_days)
    glow50.gs04()

    assert len(days) == 1  # gs04() 는 배정기를 한 번(03-10·03-11 이틀) 부른다
    d0310, _d0311 = days[0]
    assert d0310["date"] == "2026-03-10"
    assert {s: v["on_hand_end"] for s, v in d0310["sku"].items()} == {
        "X": 10,
        "Y": 5,
        "Z": 3,
    }
