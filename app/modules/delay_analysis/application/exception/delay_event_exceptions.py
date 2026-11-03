from starlette import status

from app.core.exceptions import BaseErrorCode, BusinessException


class DelayEventErrorCode(BaseErrorCode):
    """delay_analysis 모듈 지연 조회 에러 코드: (status_code, code, message)"""

    # 다른 고객사의 지연도 존재 여부를 드러내지 않도록 같은 404 로 응답한다.
    DELAY_EVENT_NOT_FOUND = (
        status.HTTP_404_NOT_FOUND,
        "DELAY_EVENT_NOT_FOUND",
        "요청한 지연 건을 찾을 수 없습니다.",
    )
    # 명세 v6.2 C20: from > to 는 입력 오류. 쿼리 파라미터 형식 오류와 같은 코드·상태(422, 응답 본문 규칙 6.1 "현재 구현")
    INVALID_DETECTED_PERIOD = (
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "VALIDATION_ERROR",
        "감지 기간의 시작(from)은 끝(to)보다 늦을 수 없습니다.",
    )


class DelayEventNotFoundException(BusinessException):
    def __init__(self):
        super().__init__(DelayEventErrorCode.DELAY_EVENT_NOT_FOUND)


class InvalidDetectedPeriodException(BusinessException):
    def __init__(self):
        super().__init__(DelayEventErrorCode.INVALID_DETECTED_PERIOD)
