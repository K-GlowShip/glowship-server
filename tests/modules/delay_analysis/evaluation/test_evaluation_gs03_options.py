import copy

import pytest
from evaluation_adapters import (
    comparison_input,
    feasibility_input,
    precheck_rows,
    ranking_rows,
)
from test_evaluation_t9_t11 import GS03_T9, GS03_T11

from app.modules.delay_analysis.application.agent.tools.option_comparison import (
    compare_options,
)
from app.modules.delay_analysis.application.agent.tools.option_feasibility import (
    check_option_feasibility,
)

"""
    GS-03 으로 정답표 대조가 틀린 값을 잡는지 본다(음성 시험). 양성 대조는 test_evaluation_t9_t11.py.
      - KR 재고 0 → T9 KR 이 NO_SUPPLY·NO_KR_ELIGIBLE_STOCK 이 돼 정답표와 달라진다.
      - KR 을 NOT_FEASIBLE 로 → T11 에서 KEEP 만 남아 1위가 돼 정답표와 달라진다.
      - 같은 입력은 같은 판정 서명(GS-03 PASS 기준 "해시가 같은 입력에서 같은 값").
"""


@pytest.fixture(scope="module")
def gs03(scenario):
    snap, _replay, analysis = scenario("GS-03")
    return snap, analysis


def test_t9_check_catches_no_kr_stock(gs03):
    snap, analysis = gs03
    wrong = copy.deepcopy(snap)
    wrong["kr_on_hand"]["CREAM20"] = 0
    rows = precheck_rows(check_option_feasibility(feasibility_input(wrong, analysis)))
    assert rows != GS03_T9
    assert rows["KR_AIR_REPLENISH"]["precheck"] == "NO_SUPPLY"
    assert rows["KR_AIR_REPLENISH"]["reason_code"] == "NO_KR_ELIGIBLE_STOCK"


def test_t11_check_catches_wrong_feasibility(gs03):
    _snap, analysis = gs03
    wrong = copy.deepcopy(analysis)
    kr = next(o for o in wrong["options"] if o["option_type"] == "KR_AIR_REPLENISH")
    kr["status"], kr["reason_code"] = "NOT_FEASIBLE", "NO_KR_ELIGIBLE_STOCK"
    rows = ranking_rows(compare_options(comparison_input(wrong)))
    assert rows != GS03_T11
    assert rows["KEEP_PLAN"] == {"rank": 1, "recommended": True}
    assert rows["KR_AIR_REPLENISH"] == {"rank": None, "recommended": False}


def test_signature_is_deterministic(glow50, gs03):
    _snap, analysis = gs03
    _snap2, _replay2, again = glow50.run_basic("GS-03")
    assert again["sha"] == analysis["sha"]
    assert again["signature"] == analysis["signature"]
