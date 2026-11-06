import copy

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
    GS-03 항구 도착 후 지연, 히트상품 한국 보충 — 정상 (02_시나리오와정답.md 5절 GS-03)
    기대값은 정답표에서 옮겼다(엔진 fixture 의 EXPECTED 가 아님).
      - 전제: 엔진이 정답표 ①(READY_FOR_REVIEW·READY_ORDER_RISK·영향 SKU CREAM20)을 낸다.
      - T9: OCEAN×AT_PORT 은 KEEP·KR·OTHER 만(PDA 행 없음), 모두 CALCULATE.
            공급일 KEEP 03-30(E)·KR 03-17(D+1+5+1)·OTHER 03-10(D), KR 긴급 항공 SKU [CREAM20].
      - T11: KR 1위 ★(추천), KEEP 2위, OTHER NOT_FEASIBLE 은 순위 없음.
      - 엔진 대조: 서버 순위 = 엔진 rank·recommended, 서버 공급일 = 엔진 supply_date.
      - 음성: 틀린 입력(KR 재고 0, KR 을 NOT_FEASIBLE 로)을 넣으면 정답표와 달라져 대조가 실패한다.
"""

ANSWER_T9 = {
    "KEEP_PLAN": {
        "precheck": "CALCULATE",
        "reason_code": None,
        "supply_available_date": "2026-03-30",
        "eligible_sku_codes": [],
    },
    "KR_AIR_REPLENISH": {
        "precheck": "CALCULATE",
        "reason_code": None,
        "supply_available_date": "2026-03-17",
        "eligible_sku_codes": ["CREAM20"],
    },
    "OTHER_REGION_FULFILL": {
        "precheck": "CALCULATE",
        "reason_code": None,
        "supply_available_date": "2026-03-10",
        "eligible_sku_codes": [],
    },
}
ANSWER_T11 = {
    "KEEP_PLAN": {"rank": 2, "recommended": False},
    "KR_AIR_REPLENISH": {"rank": 1, "recommended": True},
    "OTHER_REGION_FULFILL": {"rank": None, "recommended": False},
}


@pytest.fixture(scope="module")
def gs03(scenario):
    snap, _replay, analysis = scenario("GS-03")
    return snap, analysis


def test_engine_reproduces_answer_sheet_premise(gs03):
    _snap, analysis = gs03
    assert analysis["status"] == "READY_FOR_REVIEW"
    assert analysis["result_class"] == "READY_ORDER_RISK"
    assert sorted(analysis["k_imp"]) == ["CREAM20"]
    assert [o["option_type"] for o in analysis["options"]] == list(ANSWER_T9)


def test_t9_precheck_matches_answer_sheet(gs03):
    snap, analysis = gs03
    rows = precheck_rows(check_option_feasibility(feasibility_input(snap, analysis)))
    assert list(rows) == list(ANSWER_T9)  # 후보 행 구성·고정 순서
    assert rows == ANSWER_T9


def test_t9_supply_dates_match_engine(gs03):
    snap, analysis = gs03
    rows = precheck_rows(check_option_feasibility(feasibility_input(snap, analysis)))
    got = {k: v["supply_available_date"] for k, v in rows.items()}
    assert got == engine_supply_dates(analysis)


def test_t11_ranking_matches_answer_sheet(gs03):
    _snap, analysis = gs03
    assert ranking_rows(compare_options(comparison_input(analysis))) == ANSWER_T11


def test_t11_ranking_matches_engine(gs03):
    _snap, analysis = gs03
    got = ranking_rows(compare_options(comparison_input(analysis)))
    assert got == engine_ranking_rows(analysis)


def test_t9_check_catches_no_kr_stock(gs03):
    """KR 재고를 0 으로 바꾸면 KR 은 NO_SUPPLY·NO_KR_ELIGIBLE_STOCK 이 돼 정답표와 달라진다."""
    snap, analysis = gs03
    wrong = copy.deepcopy(snap)
    wrong["kr_on_hand"]["CREAM20"] = 0
    rows = precheck_rows(check_option_feasibility(feasibility_input(wrong, analysis)))
    assert rows != ANSWER_T9
    assert rows["KR_AIR_REPLENISH"]["precheck"] == "NO_SUPPLY"
    assert rows["KR_AIR_REPLENISH"]["reason_code"] == "NO_KR_ELIGIBLE_STOCK"


def test_t11_check_catches_wrong_feasibility(gs03):
    """KR 을 NOT_FEASIBLE 로 바꾸면 KEEP 만 남아 1위가 돼 정답표와 달라진다."""
    _snap, analysis = gs03
    wrong = copy.deepcopy(analysis)
    kr = next(o for o in wrong["options"] if o["option_type"] == "KR_AIR_REPLENISH")
    kr["status"], kr["reason_code"] = "NOT_FEASIBLE", "NO_KR_ELIGIBLE_STOCK"
    rows = ranking_rows(compare_options(comparison_input(wrong)))
    assert rows != ANSWER_T11
    assert rows["KEEP_PLAN"] == {"rank": 1, "recommended": True}
    assert rows["KR_AIR_REPLENISH"] == {"rank": None, "recommended": False}
