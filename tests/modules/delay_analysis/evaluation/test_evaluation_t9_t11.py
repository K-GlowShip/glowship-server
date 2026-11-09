import pytest
from evaluation_adapters import (
    comparison_input,
    engine_ranking_rows,
    engine_supply_dates,
    feasibility_input,
    precheck_rows,
    ranking_rows,
)

from app.modules.delay_analysis.application.agent.tools.option_comparison import (
    compare_options,
)
from app.modules.delay_analysis.application.agent.tools.option_feasibility import (
    check_option_feasibility,
)

"""
    평가 시나리오 9건의 대응안 사전 판정(T9)·추천 순위(T11)와 엔진 ①~⑤ 값 — 정답표 대조
    (02_시나리오와정답.md 3절 요약표·5절 시나리오별 표. GS-04 는 배정 시험, GS-06 은 보류라 여기 없음)
      - 기대값은 정답표에서 옮겼다(엔진 fixture 의 EXPECTED 가 아님). 공급일 = 정답표 "공급(일)" 괄호 값.
      - T9: 후보 행 구성(PDA 는 OCEAN×PRE_DEPARTURE 만, KR 은 AT_SEA·AT_PORT)과 사전 판정·공급일·긴급 항공 SKU.
      - T11: 순위와 추천(★). NOT_FEASIBLE·NEEDS_CHECK 는 순위 없음, KEEP 은 FEASIBLE 의 맨 뒤.
      - 엔진 대조: 서버 공급일 = 엔진 supply_date, 서버 순위 = 엔진 rank·recommended.
      - ①~⑤: 소진일·raw 3개·정수 준비량·품절일·위험 주문 귀속·지연 증분 회복·추천·H_end 와
        시나리오별 표의 대응안 값(공급량·확정 감소·회복 배송일·required_quantity).
      - GS-11 탐침(공여 200): OTHER 는 O2 만 회복, 지연 증분 회복 0.
"""

KEEP, PDA, KR, OTHER = (
    "KEEP_PLAN",
    "PRE_DEPARTURE_AIR",
    "KR_AIR_REPLENISH",
    "OTHER_REGION_FULFILL",
)
SKU_KEYS = (
    "depletion_date",
    "raw_baseline_shortage",
    "raw_total_shortage",
    "raw_increment_shortage",
    "baseline_prepare_quantity",
    "increment_prepare_quantity",
    "total_prepare_quantity",
    "stockout_days_total",
    "stockout_days_baseline",
    "stockout_days_increment",
)


def calc(supply_date, eligible=()):
    return {
        "precheck": "CALCULATE",
        "reason_code": None,
        "supply_available_date": supply_date,
        "eligible_sku_codes": list(eligible),
    }


def rank(n):
    return {"rank": n, "recommended": n == 1}


def sku(depletion, raw, prepare, stockout):
    """① 소진일, ② raw 기준선/전체/증분 → 정수 기준선·증분(합), ③ 품절일 전체/기준선/증분"""
    return dict(zip(SKU_KEYS, (depletion, *raw, *prepare, *stockout), strict=True))


GS03_T9 = {
    KEEP: calc("2026-03-30"),
    KR: calc("2026-03-17", ["CREAM20"]),
    OTHER: calc("2026-03-10"),
}
GS03_T11 = {KEEP: rank(2), KR: rank(1), OTHER: rank(None)}
GS03_SUMMARY = {
    "h_end": "2026-04-13",
    "skus": {
        "CREAM20": sku(
            "2026-03-10",
            ("100.0000", "200.0000", "100.0000"),
            (100, 100, 200),
            (20, 10, 10),
        )
    },
    "at_risk": {"O1": "DELAY_INDUCED"},
    "delay_recovered": {KEEP: 0, KR: 1, OTHER: 0},
    "recommended": KR,
    "options": {
        KR: {
            "supply_quantity": 200,
            "reduction_committed_total": 100,
            "recovered_delivery": {"O1": "2026-03-20"},
            "required_quantity": 200,
        },
        OTHER: {
            "feasibility_status": "NOT_FEASIBLE",
            "reason_code": "NO_TRANSFERABLE_STOCK",
        },
    },
}

ANSWERS = {
    "GS-02": {
        "t9": {
            KEEP: calc("2026-04-06"),
            PDA: calc("2026-03-20"),
            OTHER: calc("2026-03-10"),
        },
        "t11": {KEEP: rank(2), PDA: rank(1), OTHER: rank(None)},
        "summary": {
            "h_end": "2026-04-20",
            "skus": {
                "TONER50": sku(
                    "2026-03-10",
                    ("200.0000", "270.0000", "70.0000"),
                    (200, 70, 270),
                    (27, 20, 7),
                )
            },
            "at_risk": {"O1": "DELAY_INDUCED"},
            "delay_recovered": {KEEP: 0, PDA: 1, OTHER: 0},
            "recommended": PDA,
            "options": {
                PDA: {
                    "supply_quantity": 400,
                    "reduction_committed_total": 70,
                    "recovered_delivery": {"O1": "2026-03-23"},
                    "required_quantity": 270,
                },
                OTHER: {
                    "feasibility_status": "NOT_FEASIBLE",
                    "reason_code": "NO_TRANSFERABLE_STOCK",
                },
            },
        },
    },
    "GS-03": {"t9": GS03_T9, "t11": GS03_T11, "summary": GS03_SUMMARY},
    "GS-05": {
        "t9": {
            KEEP: calc("2026-03-30"),
            KR: calc("2026-03-17", ["X"]),
            OTHER: calc("2026-03-10"),
        },
        "t11": GS03_T11,
        "summary": {
            "h_end": "2026-04-13",
            "skus": {
                "X": sku(
                    "2026-03-10",
                    ("80.0000", "200.0000", "120.0000"),
                    (80, 120, 200),
                    (20, 8, 12),
                ),
                "Y": sku(
                    "2026-03-10",
                    ("24.0000", "24.0000", "0.0000"),
                    (24, 0, 24),
                    (12, 12, 0),
                ),
            },
            "at_risk": {"O1": "DELAY_INDUCED"},
            "delay_recovered": {KEEP: 0, KR: 1, OTHER: 0},
            "recommended": KR,
            "options": {
                KR: {
                    "supply_quantity": 200,
                    "reduction_committed_total": 120,
                    "recovered_delivery": {"O1": "2026-03-25"},
                    "required_quantity": 224,
                },
            },
        },
    },
    "GS-07": {
        "t9": {
            KEEP: calc("2026-03-30"),
            KR: calc("2026-03-17", ["CREAM20"]),
            OTHER: {
                "precheck": "NEEDS_CHECK",
                "reason_code": "DONOR_NO_NEXT_INBOUND",
                "supply_available_date": "2026-03-10",  # 공급 기준일 D
                "eligible_sku_codes": [],
            },
        },
        "t11": GS03_T11,
        "summary": {
            **GS03_SUMMARY,
            "delay_recovered": {KEEP: 0, KR: 1, OTHER: None},
            "options": {
                KR: {"reduction_committed_total": 100, "supply_quantity": 200},
                OTHER: {
                    "feasibility_status": "NEEDS_CHECK",
                    "reason_code": "DONOR_NO_NEXT_INBOUND",
                    "reduction_committed_total": None,
                    "supply_quantity": None,
                    "outcome_rows": None,  # 주문별 결과 행 0개
                    "rank": None,
                },
            },
        },
    },
    "GS-08": {
        "t9": {
            KEEP: calc("2026-04-01"),
            KR: {
                "precheck": "NO_SUPPLY",
                "reason_code": "NO_KR_ELIGIBLE_STOCK",
                "supply_available_date": "2026-03-17",
                "eligible_sku_codes": [],
            },
            OTHER: calc("2026-03-10"),
        },
        "t11": {KEEP: rank(1), KR: rank(None), OTHER: rank(None)},
        "summary": {
            "h_end": "2026-04-15",
            "skus": {
                "LOTION40": sku(
                    "2026-03-10",
                    ("72.0000", "132.0000", "60.0000"),
                    (72, 60, 132),
                    (22, 12, 10),
                )
            },
            "at_risk": {"O1": "DELAY_INDUCED"},
            "delay_recovered": {KEEP: 0, KR: 0, OTHER: 0},
            "recommended": KEEP,
            "options": {
                KR: {
                    "feasibility_status": "NOT_FEASIBLE",
                    "reason_code": "NO_KR_ELIGIBLE_STOCK",
                    "reduction_committed_total": 0,
                    "supply_quantity": 0,
                    "residual_prepare_total": 60,
                    "not_recovered_orders": {"O1": "NO_KR_ELIGIBLE_STOCK"},
                },
            },
        },
    },
    # GS-09 변경 A 재계산(같은 D, O2 포함)·변경 B 재계산(재고 45). 서명 대조는 test_evaluation_gs09_signature.py
    "GS-09A": {
        "t9": GS03_T9,
        "t11": GS03_T11,
        "summary": {
            "skus": {
                "CREAM20": sku(
                    "2026-03-10",
                    ("110.7143", "221.4286", "110.7143"),
                    (111, 111, 222),
                    (20, 10, 10),
                )
            },
            "at_risk": {"O1": "DELAY_INDUCED", "O2": "DELAY_INDUCED"},
            "delay_recovered": {KEEP: 0, KR: 2, OTHER: 0},
            "recommended": KR,
            "options": {
                KR: {
                    "supply_quantity": 222,
                    "reduction_committed_total": 110,
                    "recovered_orders": ["O1", "O2"],
                },
            },
        },
    },
    "GS-09B": {"t9": GS03_T9, "t11": GS03_T11, "summary": GS03_SUMMARY},
    "GS-10": {
        "t9": {
            KEEP: calc("2026-04-06"),
            KR: calc("2026-03-18", ["CREAM20"]),
            OTHER: calc("2026-03-11"),
        },
        "t11": GS03_T11,
        "summary": {
            "h_end": "2026-04-20",
            "skus": {
                "CREAM20": sku(
                    "2026-03-11",
                    ("90.0000", "260.0000", "170.0000"),
                    (90, 170, 260),
                    (26, 9, 17),
                )
            },
            "at_risk": {"O1": "DELAY_INDUCED"},
            "delay_recovered": {KEEP: 0, KR: 1, OTHER: 0},
            "recommended": KR,
            "options": {
                KR: {
                    "supply_quantity": 260,
                    "reduction_committed_total": 170,
                    "recovered_delivery": {"O1": "2026-03-21"},
                },
            },
        },
    },
    "GS-11": {
        "t9": {
            KEEP: calc("2026-03-30"),
            KR: calc("2026-03-17", ["SERUM30"]),
            OTHER: calc("2026-03-10"),
        },
        "t11": {KEEP: rank(3), KR: rank(1), OTHER: rank(2)},
        "summary": {
            "h_end": "2026-04-13",
            "skus": {
                "SERUM30": sku(
                    "2026-03-10",
                    ("100.0000", "200.0000", "100.0000"),
                    (100, 100, 200),
                    (20, 10, 10),
                )
            },
            "at_risk": {"O2": "PRE_EXISTING", "O1": "DELAY_INDUCED"},
            "delay_recovered": {KEEP: 0, KR: 1, OTHER: 1},
            "recommended": KR,
            "options": {
                KR: {
                    "supply_quantity": 200,
                    "reduction_committed_total": 100,
                    "recovered_orders": ["O1", "O2"],
                },
                OTHER: {
                    "feasibility_status": "FEASIBLE",
                    "supply_quantity": 180,
                    "reduction_committed_total": 70,
                    "residual_prepare_total": 30,
                    "recovered_orders": ["O1", "O2"],
                    "recovered_delivery": {"O1": "2026-03-16", "O2": "2026-03-16"},
                },
            },
        },
    },
}
KEYS = list(ANSWERS)


def observed_summary(glow50, snap, analysis):
    """엔진 fixture 의 summarize(저장기와 같은 사상)를 정답표 요약표 모양으로 편다."""
    s = glow50.summarize(snap, analysis)
    return {
        "h_end": s["h_end"],
        "skus": {code: {k: v[k] for k in SKU_KEYS} for code, v in s["skus"].items()},
        "at_risk": {code: v["attribution"] for code, v in s["at_risk"].items()},
        "delay_recovered": {
            ot: o["delay_induced_recovered_orders"] for ot, o in s["options"].items()
        },
        "recommended": s["recommended_option_type"],
        "options": s["options"],
    }


@pytest.mark.parametrize("key", KEYS)
def test_premise_ready_with_candidate_rows(scenario, key):
    _snap, _replay, analysis = scenario(key)
    assert analysis["status"] == "READY_FOR_REVIEW"
    assert analysis["result_class"] == "READY_ORDER_RISK"
    assert [o["option_type"] for o in analysis["options"]] == list(ANSWERS[key]["t9"])


@pytest.mark.parametrize("key", KEYS)
def test_t9_precheck_matches_answer_sheet(scenario, key):
    snap, _replay, analysis = scenario(key)
    rows = precheck_rows(check_option_feasibility(feasibility_input(snap, analysis)))
    assert list(rows) == list(ANSWERS[key]["t9"])  # 후보 행 구성·고정 순서
    assert rows == ANSWERS[key]["t9"]


@pytest.mark.parametrize("key", KEYS)
def test_t9_supply_dates_match_engine(scenario, key):
    snap, _replay, analysis = scenario(key)
    rows = precheck_rows(check_option_feasibility(feasibility_input(snap, analysis)))
    assert {
        k: v["supply_available_date"] for k, v in rows.items()
    } == engine_supply_dates(analysis)


@pytest.mark.parametrize("key", KEYS)
def test_t11_ranking_matches_answer_sheet(scenario, key):
    _snap, _replay, analysis = scenario(key)
    assert (
        ranking_rows(compare_options(comparison_input(analysis))) == ANSWERS[key]["t11"]
    )


@pytest.mark.parametrize("key", KEYS)
def test_t11_ranking_matches_engine(scenario, key):
    _snap, _replay, analysis = scenario(key)
    got = ranking_rows(compare_options(comparison_input(analysis)))
    assert got == engine_ranking_rows(analysis)


@pytest.mark.parametrize("key", KEYS)
def test_engine_values_match_answer_sheet(glow50, scenario, key):
    """①~⑤와 시나리오별 표의 대응안 값. 정답표에 있는 칸만 견준다."""
    snap, _replay, analysis = scenario(key)
    expected = ANSWERS[key]["summary"]
    got = observed_summary(glow50, snap, analysis)
    for name, value in expected.items():
        if name == "options":
            continue
        assert got[name] == value, name
    for option_type, fields in expected["options"].items():
        option = got["options"][option_type]
        assert {k: option[k] for k in fields} == fields, option_type


def test_gs11_probe_other_recovers_only_o2(scenario):
    """공여 보유 200 → 풀 80: O2(30) → O1(80) 순서라 O2 만 회복, 지연 증분 회복 0(O2 는 PRE_EXISTING)."""
    _snap, _replay, analysis = scenario("GS-11-probe")
    other = next(o for o in analysis["options"] if o["option_type"] == OTHER)
    recovered = sorted(
        x["risk"]["order_code"] for x in other["outcomes"] if x["recovered"]
    )
    not_recovered = {
        x["risk"]["order_code"]: x["reason"]
        for x in other["outcomes"]
        if not x["recovered"]
    }
    assert recovered == ["O2"]
    assert not_recovered == {"O1": "NO_TRANSFERABLE_STOCK"}
    assert other["delay_induced_recovered"] == 0
    # 탐침의 순위는 정답표에 없다. 서버 T11 이 엔진과 같은지만 본다.
    got = ranking_rows(compare_options(comparison_input(analysis)))
    assert got == engine_ranking_rows(analysis)
    assert got[KR] == rank(1)
