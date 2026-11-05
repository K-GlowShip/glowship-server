import itertools

import pytest

from app.modules.decision.application.draft_policy import (
    EXPLANATION_GENERATION_FAILED,
    DraftTarget,
    draft_rejection,
)
from app.modules.decision.application.exception.decision_exceptions import (
    DecisionErrorCode,
)
from app.modules.delay_analysis.infrastructure.analysis_enums import AnalysisRunStatus

"""
    의견·선택 초안 저장 허용 판정 (API 18 POST /analysis-runs/{runId}/drafts, 명세 v6.2 4.6절)
      - 엔진 recheck.draft_check(공유 묶음 glowship_engine/recheck.py 35~45행)를 그대로 옮긴 기준 함수와
        모든 상태 조합에서 결과를 대조합니다.
"""


def _engine_draft_check(run: dict, event_open: bool) -> tuple[bool, str | None]:
    """엔진 recheck.draft_check 를 그대로 옮긴 기준 함수 (대조용)"""
    if not event_open:
        return False, "EPISODE_CLOSED"
    if run["status"] == "VALIDATING_OUTPUT" or (
        run["status"] == "FAILED"
        and run.get("failure_code") == "EXPLANATION_GENERATION_FAILED"
    ):
        return False, "EXPLANATION_NOT_READY"
    if run["status"] != "READY_FOR_REVIEW" or not run["is_latest"]:
        return False, run["status"] if run["status"] in (
            "STALE",
            "FINALIZED",
        ) else "NOT_READY"
    return True, None


# 엔진 사유 → API 오류 코드 (명세 4.6 대응표의 제안 열, D22)
_API_CODE = {
    "EPISODE_CLOSED": DecisionErrorCode.EPISODE_CLOSED,
    "EXPLANATION_NOT_READY": DecisionErrorCode.EXPLANATION_NOT_READY,
    "STALE": DecisionErrorCode.ANALYSIS_STALE,
    "FINALIZED": DecisionErrorCode.FINAL_DECISION_EXISTS,
}


def _failure_codes(status: str) -> list[str | None]:
    """DDL ck10·ck11: ON_HOLD·FAILED 만 failure_code 가 있다"""
    if status == AnalysisRunStatus.FAILED:
        return [EXPLANATION_GENERATION_FAILED, "TOOL_TIMEOUT_EXHAUSTED"]
    if status == AnalysisRunStatus.ON_HOLD:
        return ["REQUIRED_DATA_INVALID"]
    return [None]


_CASES = [
    DraftTarget(status, is_latest, failure_code, episode_open)
    for status in AnalysisRunStatus
    for failure_code in _failure_codes(status)
    for is_latest, episode_open in itertools.product([True, False], repeat=2)
]


@pytest.mark.parametrize("target", _CASES, ids=str)
def test_draft_rejection_matches_engine(target):
    allowed, reason = _engine_draft_check(
        {
            "status": str(target.status),
            "is_latest": target.is_latest,
            "failure_code": target.failure_code,
        },
        target.episode_open,
    )

    got = draft_rejection(target)

    if allowed:
        assert got is None
    elif reason == "NOT_READY":
        # 엔진 NOT_READY 를 API 코드 둘로 나눔: 현재 실행이 아니면 NOT_CURRENT_RUN
        assert got == (
            DecisionErrorCode.RUN_NOT_READY
            if target.is_latest
            else DecisionErrorCode.NOT_CURRENT_RUN
        )
    else:
        assert got == _API_CODE[reason]


def test_only_open_latest_ready_for_review_is_allowed():
    allowed = [target for target in _CASES if draft_rejection(target) is None]

    assert allowed == [
        DraftTarget(AnalysisRunStatus.READY_FOR_REVIEW, True, None, True)
    ]


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        # 종료된 에피소드가 가장 먼저: 선택 대기 현재 실행이어도 거부
        (
            DraftTarget(AnalysisRunStatus.READY_FOR_REVIEW, True, None, False),
            DecisionErrorCode.EPISODE_CLOSED,
        ),
        # 설명 생성 중
        (
            DraftTarget(AnalysisRunStatus.VALIDATING_OUTPUT, True, None, True),
            DecisionErrorCode.EXPLANATION_NOT_READY,
        ),
        # 설명 생성만 실패
        (
            DraftTarget(
                AnalysisRunStatus.FAILED, True, EXPLANATION_GENERATION_FAILED, True
            ),
            DecisionErrorCode.EXPLANATION_NOT_READY,
        ),
        # 도구 실패는 상태가 맞지 않는 것
        (
            DraftTarget(AnalysisRunStatus.FAILED, True, "TOOL_TIMEOUT_EXHAUSTED", True),
            DecisionErrorCode.RUN_NOT_READY,
        ),
        # 이전 버전이 된 STALE 실행은 NOT_CURRENT_RUN 보다 STALE 이 먼저 (엔진 순서)
        (
            DraftTarget(AnalysisRunStatus.STALE, False, None, True),
            DecisionErrorCode.ANALYSIS_STALE,
        ),
        (
            DraftTarget(AnalysisRunStatus.FINALIZED, True, None, True),
            DecisionErrorCode.FINAL_DECISION_EXISTS,
        ),
        # 재분석으로 is_latest 만 내려간 선택 대기 실행
        (
            DraftTarget(AnalysisRunStatus.READY_FOR_REVIEW, False, None, True),
            DecisionErrorCode.NOT_CURRENT_RUN,
        ),
        (
            DraftTarget(AnalysisRunStatus.NO_RISK, True, None, True),
            DecisionErrorCode.RUN_NOT_READY,
        ),
    ],
)
def test_draft_rejection_examples(target, expected):
    assert draft_rejection(target) == expected


def test_error_codes_follow_spec_status():
    """명세 4.6·5절: 초안 거부는 409, 다른 실행의 대응안은 422, 없는 실행은 404"""
    conflict = [
        DecisionErrorCode.EPISODE_CLOSED,
        DecisionErrorCode.EXPLANATION_NOT_READY,
        DecisionErrorCode.ANALYSIS_STALE,
        DecisionErrorCode.FINAL_DECISION_EXISTS,
        DecisionErrorCode.NOT_CURRENT_RUN,
        DecisionErrorCode.RUN_NOT_READY,
    ]
    assert {code.status_code for code in conflict} == {409}
    assert all(code.code == code.name for code in DecisionErrorCode)
    assert DecisionErrorCode.OPTION_NOT_IN_RUN.status_code == 422
    assert DecisionErrorCode.ANALYSIS_RUN_NOT_FOUND.status_code == 404
