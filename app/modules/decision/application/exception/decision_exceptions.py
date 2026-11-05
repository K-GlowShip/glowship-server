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
    OPTION_NOT_IN_RUN = (
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "OPTION_NOT_IN_RUN",
        "이 분석 실행의 대응안이 아닙니다.",
    )


class DecisionException(BusinessException):
    """판단 API 거부. 어떤 코드인지는 error_code 로 구분합니다."""

    def __init__(self, error_code: DecisionErrorCode):
        super().__init__(error_code)
