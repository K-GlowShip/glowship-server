from starlette import status

from app.core.exceptions import BaseErrorCode, BusinessException

"""
    운영 기준 API 오류 코드 (API 명세 v6.2 4.2절)
      - 배정되지 않은 고객사는 공통 403 FORBIDDEN, 비활성 고객사는 company 모듈의 409 COMPANY_INACTIVE 를 씁니다.
"""


class OperatingRuleErrorCode(BaseErrorCode):
    """operating_rule 모듈 에러 코드: (status_code, code, message)"""

    INVALID_LEAD_TIME = (
        status.HTTP_400_BAD_REQUEST,
        "INVALID_LEAD_TIME",
        "리드타임이 규칙에 맞지 않습니다.",
    )


class InvalidLeadTimeException(BusinessException):
    """리드타임 규칙 위반. 어느 규칙인지는 reason 에 담아 서버 로그·시험에서 구분합니다(응답 문구는 하나)."""

    def __init__(self, reason: str):
        super().__init__(OperatingRuleErrorCode.INVALID_LEAD_TIME)
        self.reason = reason
