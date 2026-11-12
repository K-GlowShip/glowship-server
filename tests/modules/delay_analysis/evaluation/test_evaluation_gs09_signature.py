from datetime import date

import pytest

from app.modules.decision.application.exception.decision_exceptions import (
    DecisionErrorCode,
)
from app.modules.decision.application.selection_policy import (
    LeadTimes,
    SelectionOption,
    SelectionTarget,
    selection_rejection,
)
from app.modules.delay_analysis.infrastructure.analysis_enums import (
    FeasibilityStatus,
    OptionType,
)

"""
    GS-09 일반 데이터 변경 → 서명 재계산 → STALE 여부 (02_시나리오와정답.md 5절 GS-09)
      - R1 = GS-03. 변경 A(늦게 도착한 O2, 03-11 수집) → RELEVANT_ORDER_CHANGED, 서명 다름 → STALE.
        변경 B(재고 50 → 45) → RELEVANT_INVENTORY_CHANGED, 서명 같음 → READY_FOR_REVIEW 유지.
      - 재계산 값(A: 111·111, 222, 110, O1·O2 DELAY_INDUCED, KR 회복 2)은 test_evaluation_t9_t11.py GS-09A.
      - 엔진 1차 필터(stale.first_filter)와 서명 비교까지만 본다. 서버 StaleJudgmentService 대조는 2단계(DB 필요).
      - 정답표 "R1 에 대한 최종 선택 시도는 거부"(변경 A): 엔진이 낸 R1 상태를 서버 최종 선택 판정
        (selection_policy.selection_rejection, API 19 재확인)에 넣는다. A·B 는 상태만 다르고 나머지 입력은 같은
        "선택할 수 있는 값"이라, 거부가 갈리는 이유는 STALE 하나다. 실제 경로가 남긴 STALE 실행을 서비스가
        거부하는 것은 2단계(test_evaluation_stale_answer 연결 확인, test_evaluation_runner_gaps B5)가 본다.
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


# 변경 → R1 최종 선택 판정. A 는 409 ANALYSIS_STALE, B 는 거부 없음(선택 가능)
R1_SELECTION = {"A": DecisionErrorCode.ANALYSIS_STALE, "B": None}


def _r1_selection_target(status: str) -> SelectionTarget:
    """R1 의 선택 대상. 상태만 시나리오에서 오고, 나머지는 다른 거부 사유에 걸리지 않는 값이다."""
    return SelectionTarget(
        status=status,
        is_latest=True,
        failure_code=None,
        episode_open=True,
        operating_rule_set_id="RULE-1",
        active_operating_rule_set_id="RULE-1",
        option=SelectionOption(OptionType.KEEP_PLAN, FeasibilityStatus.FEASIBLE, ()),
        lead_times=LeadTimes(0, 0, 0, 0, 0, 0, 0),
        latest_expected_3pl_inbound_date=date(2026, 3, 30),
        planned_kr_release_date=date(2026, 3, 10),
        selection_day=date(2026, 3, 11),
    )


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


@pytest.mark.parametrize("change", ["A", "B"])
def test_r1_final_selection_matches_answer_sheet(gs09, change):
    """정답표 GS-09: 변경 A 뒤 R1 최종 선택 시도는 거부(409 ANALYSIS_STALE), 변경 B 뒤에는 막지 않는다."""
    _snap1, _r1, changes = gs09
    status = changes[change]["r1_status"]

    rejection = selection_rejection(_r1_selection_target(status))

    assert status == EXPECTED[change]["r1_status"]
    assert (rejection.code if rejection else None) == R1_SELECTION[change]
    if rejection is not None:
        assert (rejection.code.code, rejection.code.status_code) == (
            "ANALYSIS_STALE",
            409,
        )
