from dataclasses import dataclass

from app.modules.decision.application.exception.decision_exceptions import (
    DecisionErrorCode,
)
from app.modules.delay_analysis.infrastructure.analysis_enums import AnalysisRunStatus

"""
    의견·선택 초안 저장 허용 판정 (API 18 POST /analysis-runs/{runId}/drafts, API 명세 v6.2 4.6절)
      - 엔진 recheck.draft_check 와 같은 순서로 확인합니다: 에피소드 종료 → 설명 미준비 → STALE·FINALIZED → 현재 실행·선택 대기.
      - 엔진의 NOT_READY 하나를 API 코드 둘로 나눕니다: 현재 실행이 아니면 NOT_CURRENT_RUN, 현재 실행인데 상태가 맞지 않으면 RUN_NOT_READY.
      - 대응안 소속(OPTION_NOT_IN_RUN)은 DB 조회가 필요해 서비스가 이 판정 뒤에 확인합니다.
        대응안의 실행 가능 여부(FEASIBLE·NEEDS_CHECK·NOT_FEASIBLE)는 보지 않습니다(C24).
      - 요청자 역할(CLIENT_MEMBER, D17)은 라우터 의존성이 먼저 거릅니다.
"""

# 설명 생성만 실패한 실행의 failure_code (엔진 recheck.py, 명세 4.5 상태별 표)
EXPLANATION_GENERATION_FAILED = "EXPLANATION_GENERATION_FAILED"


@dataclass(frozen=True)
class DraftTarget:
    """초안 판정에 필요한 실행·에피소드 상태"""

    status: str
    is_latest: bool
    failure_code: str | None
    episode_open: bool  # delay_events.closed_at 이 없음


def draft_rejection(target: DraftTarget) -> DecisionErrorCode | None:
    """초안을 거부할 오류 코드. 허용이면 None."""
    if not target.episode_open:
        return DecisionErrorCode.EPISODE_CLOSED
    if target.status == AnalysisRunStatus.VALIDATING_OUTPUT or (
        target.status == AnalysisRunStatus.FAILED
        and target.failure_code == EXPLANATION_GENERATION_FAILED
    ):
        return DecisionErrorCode.EXPLANATION_NOT_READY
    if target.status == AnalysisRunStatus.STALE:
        return DecisionErrorCode.ANALYSIS_STALE
    if target.status == AnalysisRunStatus.FINALIZED:
        return DecisionErrorCode.FINAL_DECISION_EXISTS
    if not target.is_latest:
        return DecisionErrorCode.NOT_CURRENT_RUN
    if target.status != AnalysisRunStatus.READY_FOR_REVIEW:
        return DecisionErrorCode.RUN_NOT_READY
    return None
