from starlette import status

from app.core.exceptions import BaseErrorCode, BusinessException


class OrderErrorCode(BaseErrorCode):
    """order 모듈 에러 코드: (status_code, code, message)"""

    # 쿼리 파라미터 형식 오류와 같은 코드(VALIDATION_ERROR)·상태(422, 응답 본문 규칙 6.1 "현재 구현")로 응답한다.
    INVALID_ORDERED_PERIOD = (
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "VALIDATION_ERROR",
        "주문 기간의 시작(from)은 끝(to)보다 늦을 수 없습니다.",
    )


class InvalidOrderedPeriodException(BusinessException):
    def __init__(self):
        super().__init__(OrderErrorCode.INVALID_ORDERED_PERIOD)
