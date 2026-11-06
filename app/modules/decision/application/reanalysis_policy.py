from app.modules.decision.application.draft_policy import DraftTarget
from app.modules.decision.application.exception.decision_exceptions import (
    DecisionErrorCode,
)
from app.modules.delay_analysis.application.stale.stale_service import (
    IN_PROGRESS_STATUSES,
)
from app.modules.delay_analysis.infrastructure.analysis_enums import AnalysisRunStatus

"""
    재분석 허용 판정 (API 20 POST /analysis-runs/{runId}/reanalysis, API 명세 v6.2 4.6절·D10)
      - 판정 입력은 초안과 같은 실행 상태·현재 실행 여부·에피소드 열림(DraftTarget)입니다. 실패 코드는 보지 않습니다
        (ON_HOLD·FAILED 는 실패 코드와 관계없이 재분석 허용).
      - 순서: 에피소드 종료(엔진 하네스 run_s1_s5.py 의 재분석 거부 사유가 이것 하나) → 현재 실행 → 진행 중
        → FINALIZED → NO_DELAY. 허용 상태는 READY_FOR_REVIEW·STALE·NO_RISK·ON_HOLD·FAILED (D10).
      - 요청자 역할(팀원·팀장, 명세 6절)은 라우터 의존성이, 없는 실행 404 는 서비스가 먼저 거릅니다.
"""

REANALYSIS_ALLOWED_STATUSES = frozenset(
    {
        AnalysisRunStatus.READY_FOR_REVIEW,
        AnalysisRunStatus.STALE,
        AnalysisRunStatus.NO_RISK,
        AnalysisRunStatus.ON_HOLD,
        AnalysisRunStatus.FAILED,
    }
)


def reanalysis_rejection(target: DraftTarget) -> DecisionErrorCode | None:
    """재분석을 거부할 오류 코드. 허용이면 None."""
    if not target.episode_open:
        return DecisionErrorCode.EPISODE_CLOSED
    if not target.is_latest:
        return DecisionErrorCode.NOT_CURRENT_RUN
    if target.status in IN_PROGRESS_STATUSES:
        return DecisionErrorCode.RUN_IN_PROGRESS
    if target.status == AnalysisRunStatus.FINALIZED:
        return DecisionErrorCode.RUN_FINALIZED
    if target.status not in REANALYSIS_ALLOWED_STATUSES:
        return DecisionErrorCode.REANALYSIS_NOT_ALLOWED
    return None
