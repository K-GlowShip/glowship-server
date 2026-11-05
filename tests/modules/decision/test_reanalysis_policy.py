import itertools

import pytest

from app.modules.decision.application.draft_policy import DraftTarget
from app.modules.decision.application.exception.decision_exceptions import (
    DecisionErrorCode,
)
from app.modules.decision.application.reanalysis_policy import (
    REANALYSIS_ALLOWED_STATUSES,
    reanalysis_rejection,
)
from app.modules.delay_analysis.infrastructure.analysis_enums import AnalysisRunStatus

"""
    재분석 허용 판정 (API 20, 명세 v6.2 4.6절·D10)
"""

IN_PROGRESS = {
    AnalysisRunStatus.DETECTED,
    AnalysisRunStatus.VALIDATING,
    AnalysisRunStatus.ANALYZING_IMPACT,
    AnalysisRunStatus.ANALYZING_OPTIONS,
    AnalysisRunStatus.VALIDATING_OUTPUT,
}


def _failure_code(status):
    """DDL ck10·ck11: ON_HOLD·FAILED 만 실패 코드가 있다 (재분석 판정은 보지 않음)"""
    return {
        AnalysisRunStatus.ON_HOLD: "REQUIRED_DATA_INVALID",
        AnalysisRunStatus.FAILED: "EXPLANATION_GENERATION_FAILED",
    }.get(status)


_CASES = [
    DraftTarget(status, is_latest, _failure_code(status), episode_open)
    for status in AnalysisRunStatus
    for is_latest, episode_open in itertools.product([True, False], repeat=2)
]


def _expected(target: DraftTarget) -> DecisionErrorCode | None:
    """명세 4.6·D10 을 그대로 옮긴 기준"""
    if not target.episode_open:
        return DecisionErrorCode.EPISODE_CLOSED
    if not target.is_latest:
        return DecisionErrorCode.NOT_CURRENT_RUN
    if target.status in IN_PROGRESS:
        return DecisionErrorCode.RUN_IN_PROGRESS
    if target.status == AnalysisRunStatus.FINALIZED:
        return DecisionErrorCode.RUN_FINALIZED
    if target.status == AnalysisRunStatus.NO_DELAY:
        return DecisionErrorCode.REANALYSIS_NOT_ALLOWED
    return None


@pytest.mark.parametrize("target", _CASES, ids=str)
def test_reanalysis_rejection_for_every_state(target):
    assert reanalysis_rejection(target) == _expected(target)


def test_allowed_statuses_are_d10():
    """D10: READY_FOR_REVIEW·STALE·NO_RISK·ON_HOLD·FAILED 만 허용. 12개 상태를 빠짐없이 나눈다"""
    allowed = {
        target.status
        for target in _CASES
        if target.is_latest
        and target.episode_open
        and reanalysis_rejection(target) is None
    }

    assert allowed == {
        AnalysisRunStatus.READY_FOR_REVIEW,
        AnalysisRunStatus.STALE,
        AnalysisRunStatus.NO_RISK,
        AnalysisRunStatus.ON_HOLD,
        AnalysisRunStatus.FAILED,
    }
    assert allowed == REANALYSIS_ALLOWED_STATUSES
    rejected = set(AnalysisRunStatus) - allowed
    assert rejected == IN_PROGRESS | {
        AnalysisRunStatus.FINALIZED,
        AnalysisRunStatus.NO_DELAY,
    }


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        # 엔진 하네스: 종료된 에피소드는 다른 조건보다 먼저 거부
        (
            DraftTarget(AnalysisRunStatus.STALE, False, None, False),
            DecisionErrorCode.EPISODE_CLOSED,
        ),
        # 이전 버전은 허용 상태여도 거부
        (
            DraftTarget(AnalysisRunStatus.STALE, False, None, True),
            DecisionErrorCode.NOT_CURRENT_RUN,
        ),
        (
            DraftTarget(AnalysisRunStatus.VALIDATING_OUTPUT, True, None, True),
            DecisionErrorCode.RUN_IN_PROGRESS,
        ),
        (
            DraftTarget(AnalysisRunStatus.FINALIZED, True, None, True),
            DecisionErrorCode.RUN_FINALIZED,
        ),
        (
            DraftTarget(AnalysisRunStatus.NO_DELAY, True, None, True),
            DecisionErrorCode.REANALYSIS_NOT_ALLOWED,
        ),
        # 설명 생성만 실패한 FAILED 도 재분석은 허용 (초안과 다름)
        (
            DraftTarget(
                AnalysisRunStatus.FAILED, True, "EXPLANATION_GENERATION_FAILED", True
            ),
            None,
        ),
        (
            DraftTarget(AnalysisRunStatus.ON_HOLD, True, "REQUIRED_DATA_INVALID", True),
            None,
        ),
    ],
)
def test_reanalysis_rejection_examples(target, expected):
    assert reanalysis_rejection(target) == expected


def test_new_codes_are_409():
    """명세 4.6 재분석 오류는 모두 409"""
    for code in (
        DecisionErrorCode.RUN_IN_PROGRESS,
        DecisionErrorCode.RUN_FINALIZED,
        DecisionErrorCode.REANALYSIS_NOT_ALLOWED,
    ):
        assert (code.status_code, code.code) == (409, code.name)
