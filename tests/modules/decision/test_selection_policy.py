import itertools
from datetime import date, timedelta

import pytest

from app.modules.decision.application.exception.decision_exceptions import (
    DecisionErrorCode,
)
from app.modules.decision.application.selection_policy import (
    LeadTimes,
    SelectionOption,
    SelectionRejection,
    SelectionTarget,
    selection_rejection,
)
from app.modules.delay_analysis.infrastructure.analysis_enums import (
    AnalysisRunStatus,
    FeasibilityStatus,
    OptionType,
)

"""
    최종 선택 허용 판정 (API 19, 명세 v6.2 4.6절 재확인 2~7)
      - 엔진 recheck.selection_check(공유 묶음 glowship_engine/recheck.py 6~32행)를 그대로 옮긴 기준 함수와
        상태·운영 기준·대응안·선택일 조합에서 결과를 대조합니다. 아래 날짜·리드타임은 시험용 값입니다.
"""


def _engine_selection_check(
    run, option, selection_day, latest_E, active_rule_id, event_open
):
    """엔진 recheck.selection_check 를 그대로 옮긴 기준 함수 (대조용)"""
    if not event_open:
        return False, "EPISODE_CLOSED", False
    if (
        run["status"] == "FAILED"
        and run.get("failure_code") == "EXPLANATION_GENERATION_FAILED"
    ):
        return False, "EXPLANATION_NOT_READY", False
    if run["status"] != "READY_FOR_REVIEW" or not run["is_latest"]:
        return (
            False,
            run["status"] if run["status"] in ("STALE", "FINALIZED") else "NOT_READY",
            False,
        )
    if run["operating_rule_set_id"] != active_rule_id:
        return False, "OPERATING_RULE_VERSION_CHANGED", False
    if option is None or option["status"] != "FEASIBLE":
        return False, "OPTION_NOT_FEASIBLE", False
    lt, t = run["lt"], selection_day
    ot = option["option_type"]
    ok = True
    if ot == "KR_AIR_REPLENISH":
        ok = t + timedelta(lt["oh_kr"] + lt["air_kr"] + lt["inbound_dest"]) < latest_E
    elif ot == "PRE_DEPARTURE_AIR":
        ok = (
            max(t, run["planned_kr_release"])
            + timedelta(lt["oh_kr"] + lt["air_kr"] + lt["inbound_dest"])
            < latest_E
        )
    elif ot == "OTHER_REGION_FULFILL":
        ok = all(
            t + timedelta(lt["oh_donor"] + lt["cd_cross"]) <= x["risk"]["promised"]
            for x in option["outcomes"] or []
            if x["recovered"]
        )
    if ok and ot != "KEEP_PLAN":
        ok = all(
            t <= x["risk"]["promised"] - timedelta(lt["oh_dest"] + lt["cd_same"])
            for x in option["outcomes"] or []
            if x["recovered"]
        )
    if not ok:
        return False, "TIME_ELAPSED", True
    return True, None, False


LT = LeadTimes(
    oh_kr=1, air_kr=3, inbound_dest=2, oh_donor=1, cd_cross=4, oh_dest=1, cd_same=2
)
LATEST_E = date(2026, 7, 20)
PLANNED_KR_RELEASE = date(2026, 7, 5)
PROMISED = (date(2026, 7, 12), date(2026, 7, 15))
RULE, OTHER_RULE = "rule-v3", "rule-v4"
KEEP_PLAN = SelectionOption(OptionType.KEEP_PLAN, FeasibilityStatus.FEASIBLE, PROMISED)

# 엔진 사유 → API 오류 코드 (명세 4.6 대응표 D22 제안, 11/05 결정)
_API_CODE = {
    "EPISODE_CLOSED": DecisionErrorCode.EPISODE_CLOSED,
    "EXPLANATION_NOT_READY": DecisionErrorCode.EXPLANATION_NOT_READY,
    "STALE": DecisionErrorCode.ANALYSIS_STALE,
    "FINALIZED": DecisionErrorCode.FINAL_DECISION_EXISTS,
    "OPERATING_RULE_VERSION_CHANGED": DecisionErrorCode.OPERATING_RULE_CHANGED,
    "OPTION_NOT_FEASIBLE": DecisionErrorCode.OPTION_NOT_FEASIBLE,
    "TIME_ELAPSED": DecisionErrorCode.ANALYSIS_STALE_TIME_ELAPSED,
}


def _target(
    status=AnalysisRunStatus.READY_FOR_REVIEW,
    is_latest=True,
    failure_code=None,
    episode_open=True,
    active_rule=RULE,
    option=KEEP_PLAN,
    selection_day=date(2026, 7, 2),
) -> SelectionTarget:
    return SelectionTarget(
        status=status,
        is_latest=is_latest,
        failure_code=failure_code,
        episode_open=episode_open,
        operating_rule_set_id=RULE,
        active_operating_rule_set_id=active_rule,
        option=option,
        lead_times=LT,
        latest_expected_3pl_inbound_date=LATEST_E,
        planned_kr_release_date=PLANNED_KR_RELEASE,
        selection_day=selection_day,
    )


def _engine(target: SelectionTarget):
    option = target.option
    return _engine_selection_check(
        {
            "status": str(target.status),
            "is_latest": target.is_latest,
            "failure_code": target.failure_code,
            "operating_rule_set_id": target.operating_rule_set_id,
            "lt": vars(target.lead_times),
            "planned_kr_release": target.planned_kr_release_date,
        },
        None
        if option is None
        else {
            "option_type": str(option.option_type),
            "status": str(option.feasibility_status),
            "outcomes": [
                {"recovered": True, "risk": {"promised": p}}
                for p in option.recovered_promised_dates
            ],
        },
        target.selection_day,
        target.latest_expected_3pl_inbound_date,
        target.active_operating_rule_set_id,
        target.episode_open,
    )


def _failure_codes(status):
    if status == AnalysisRunStatus.FAILED:
        return [None, "EXPLANATION_GENERATION_FAILED", "TOOL_TIMEOUT_EXHAUSTED"]
    return [None]


_STATE_CASES = [
    _target(status=s, is_latest=latest, failure_code=f, episode_open=open_)
    for s in AnalysisRunStatus
    for f in _failure_codes(s)
    for latest, open_ in itertools.product([True, False], repeat=2)
]
_OPTION_CASES = [
    _target(
        option=None if kind is None else SelectionOption(kind, feasibility, promised),
        active_rule=rule,
        selection_day=date(2026, 7, 1) + timedelta(days=offset),
    )
    for kind in [None, *OptionType]
    for feasibility in FeasibilityStatus
    for promised in [(), PROMISED, (date(2026, 7, 4),)]
    for rule in [RULE, OTHER_RULE]
    for offset in range(0, 16, 3)
]


@pytest.mark.parametrize("target", _STATE_CASES + _OPTION_CASES)
def test_selection_rejection_matches_engine(target):
    allowed, reason, time_elapsed = _engine(target)

    got = selection_rejection(target)

    if allowed:
        assert got is None
    elif reason == "NOT_READY":
        expected = (
            DecisionErrorCode.RUN_NOT_READY
            if target.is_latest
            else DecisionErrorCode.NOT_CURRENT_RUN
        )
        assert got == SelectionRejection(expected)
    elif reason == "OPTION_NOT_FEASIBLE" and target.option is None:
        # 다른 실행의 대응안은 API 18 과 같은 OPTION_NOT_IN_RUN 으로 나눔 (11/05 결정)
        assert got == SelectionRejection(DecisionErrorCode.OPTION_NOT_IN_RUN)
    else:
        assert got == SelectionRejection(_API_CODE[reason], time_elapsed=time_elapsed)


def test_some_cases_pass_and_some_hit_time_elapsed():
    """대조 조합이 허용·시간 경과를 모두 포함하는지 (식이 실제로 비교되었는지)"""
    results = [selection_rejection(t) for t in _OPTION_CASES]

    assert any(r is None for r in results)
    assert any(r is not None and r.time_elapsed for r in results)


@pytest.mark.parametrize(
    ("kind", "promised", "day", "allowed"),
    [
        # 한국 창고 항공(회복 주문 없음): 선택일 + 1 + 3 + 2 < 최신 E(07-20) → 07-13 까지
        (OptionType.KR_AIR_REPLENISH, (), date(2026, 7, 13), True),
        (OptionType.KR_AIR_REPLENISH, (), date(2026, 7, 14), False),
        # 회복 주문이 있으면 같은 권역 조건(선택일 ≤ 가장 이른 약속일 07-12 − 1 − 2)도 봄
        (OptionType.KR_AIR_REPLENISH, PROMISED, date(2026, 7, 9), True),
        (OptionType.KR_AIR_REPLENISH, PROMISED, date(2026, 7, 10), False),
        # 출항 전 항공: max(선택일, 한국 출고 계획일 07-05) + 6 < 07-20
        (OptionType.PRE_DEPARTURE_AIR, (), date(2026, 7, 1), True),
        (OptionType.PRE_DEPARTURE_AIR, (), date(2026, 7, 14), False),
        # 다른 권역: 선택일 + 1 + 4 ≤ 07-12 이고 선택일 ≤ 07-09
        (OptionType.OTHER_REGION_FULFILL, PROMISED, date(2026, 7, 7), True),
        (OptionType.OTHER_REGION_FULFILL, PROMISED, date(2026, 7, 8), False),
        # 계획 유지는 시간 확인 없음
        (OptionType.KEEP_PLAN, PROMISED, date(2026, 8, 31), True),
    ],
)
def test_time_check_boundaries(kind, promised, day, allowed):
    target = _target(
        option=SelectionOption(kind, FeasibilityStatus.FEASIBLE, promised),
        selection_day=day,
    )

    got = selection_rejection(target)

    if allowed:
        assert got is None
    else:
        assert got == SelectionRejection(
            DecisionErrorCode.ANALYSIS_STALE_TIME_ELAPSED, time_elapsed=True
        )


def test_needs_check_option_is_draft_only():
    """C24: NEEDS_CHECK·NOT_FEASIBLE 은 초안만, 최종 선택은 FEASIBLE 만"""
    for feasibility in (FeasibilityStatus.NEEDS_CHECK, FeasibilityStatus.NOT_FEASIBLE):
        target = _target(
            option=SelectionOption(
                OptionType.OTHER_REGION_FULFILL, feasibility, PROMISED
            )
        )
        assert selection_rejection(target) == SelectionRejection(
            DecisionErrorCode.OPTION_NOT_FEASIBLE
        )


def test_new_codes():
    assert (
        DecisionErrorCode.OPERATING_RULE_CHANGED.status_code,
        DecisionErrorCode.OPERATING_RULE_CHANGED.code,
    ) == (409, "OPERATING_RULE_CHANGED")
    assert (
        DecisionErrorCode.OPTION_NOT_FEASIBLE.status_code,
        DecisionErrorCode.OPTION_NOT_FEASIBLE.code,
    ) == (422, "OPTION_NOT_FEASIBLE")
    # 시간 경과는 코드 ANALYSIS_STALE 그대로, 문구에 TIME_ELAPSED (명세 4.6 대응표)
    elapsed = DecisionErrorCode.ANALYSIS_STALE_TIME_ELAPSED
    assert (elapsed.status_code, elapsed.code) == (409, "ANALYSIS_STALE")
    assert "TIME_ELAPSED" in elapsed.message
