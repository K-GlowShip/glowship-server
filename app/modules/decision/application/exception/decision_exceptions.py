from starlette import status

from app.core.exceptions import BaseErrorCode, BusinessException

"""
    판단(초안·재분석·최종 선택) API 오류 코드 (API 명세 v6.2 4.6절, 5절)
      - 엔진 거부 사유(recheck.py)와 API 코드의 대응은 4.6 "엔진 거부 사유와 API 오류 코드 대응" 표의 제안 열을 따릅니다(D22).
      - EPISODE_CLOSED·EXPLANATION_NOT_READY 는 그 표에서 새로 두는 코드입니다.
      - 없는 실행·다른 고객사 실행의 404 는 API 17 과 같은 AnalysisRunNotFoundException(delay_analysis)을 씁니다.
"""


class DecisionErrorCode(BaseErrorCode):
    """decision 모듈 에러 코드: (status_code, code, message)"""

    EPISODE_CLOSED = (
        status.HTTP_409_CONFLICT,
        "EPISODE_CLOSED",
        "지연이 종료(입고·회복)되어 판단을 기록할 수 없습니다.",
    )
    EXPLANATION_NOT_READY = (
        status.HTTP_409_CONFLICT,
        "EXPLANATION_NOT_READY",
        "분석 설명이 아직 없거나 생성에 실패해 판단을 기록할 수 없습니다.",
    )
    ANALYSIS_STALE = (
        status.HTTP_409_CONFLICT,
        "ANALYSIS_STALE",
        "분석 뒤 관련 데이터가 바뀌어 재분석이 필요합니다.",
    )
    FINAL_DECISION_EXISTS = (
        status.HTTP_409_CONFLICT,
        "FINAL_DECISION_EXISTS",
        "이미 최종 선택된 분석 실행입니다.",
    )
    NOT_CURRENT_RUN = (
        status.HTTP_409_CONFLICT,
        "NOT_CURRENT_RUN",
        "현재 분석 실행이 아닙니다. 최신 분석 결과에서 다시 시도해 주세요.",
    )
    RUN_NOT_READY = (
        status.HTTP_409_CONFLICT,
        "RUN_NOT_READY",
        "선택 대기(READY_FOR_REVIEW) 상태의 분석 실행이 아닙니다.",
    )
    # 재분석(API 20) 거부: 명세 4.6 POST /analysis-runs/{runId}/reanalysis 오류, 허용 상태는 D10
    RUN_IN_PROGRESS = (
        status.HTTP_409_CONFLICT,
        "RUN_IN_PROGRESS",
        "분석이 진행 중입니다. 끝난 뒤 다시 요청해 주세요.",
    )
    RUN_FINALIZED = (
        status.HTTP_409_CONFLICT,
        "RUN_FINALIZED",
        "이미 최종 선택이 끝난 분석 실행은 재분석할 수 없습니다.",
    )
    REANALYSIS_NOT_ALLOWED = (
        status.HTTP_409_CONFLICT,
        "REANALYSIS_NOT_ALLOWED",
        "지연이 해소된(NO_DELAY) 분석 실행은 재분석할 수 없습니다.",
    )
    # 최종 선택(API 19) 거부: 명세 4.6 POST /analysis-runs/{runId}/final-decision, 대응표 D22 제안
    OPERATING_RULE_CHANGED = (
        status.HTTP_409_CONFLICT,
        "OPERATING_RULE_CHANGED",
        "분석 뒤 운영 기준이 바뀌어 재분석이 필요합니다.",
    )
    # 시간 경과(TIME_ELAPSED)는 코드는 ANALYSIS_STALE 그대로, 사유를 문구에 둡니다(명세 4.6 대응표)
    ANALYSIS_STALE_TIME_ELAPSED = (
        status.HTTP_409_CONFLICT,
        "ANALYSIS_STALE",
        "선택 시점에 대응안이 시간 안에 들어오지 않습니다(TIME_ELAPSED). 재분석이 필요합니다.",
    )
    OPTION_NOT_FEASIBLE = (
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "OPTION_NOT_FEASIBLE",
        "실행 가능(FEASIBLE)한 대응안만 최종 선택할 수 있습니다.",
    )
    # 같은 고객사에서 이미 다른 실행에 쓴 Idempotency-Key (final_decisions_uk3). 명세에 없는 코드(11/05 결정, 명세 반영은 요청)
    IDEMPOTENCY_KEY_REUSED = (
        status.HTTP_409_CONFLICT,
        "IDEMPOTENCY_KEY_REUSED",
        "이 Idempotency-Key 는 다른 분석 실행의 최종 선택에 이미 쓰였습니다. 새 키로 요청해 주세요.",
    )
    OPTION_NOT_IN_RUN = (
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "OPTION_NOT_IN_RUN",
        "이 분석 실행의 대응안이 아닙니다.",
    )


class DecisionException(BusinessException):
    """판단 API 거부. 어떤 코드인지는 error_code 로 구분합니다."""

    def __init__(self, error_code: DecisionErrorCode):
        super().__init__(error_code)
