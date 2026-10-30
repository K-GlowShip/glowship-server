from starlette import status

from app.core.exceptions import BaseErrorCode, BusinessException


class CompanyErrorCode(BaseErrorCode):
    """company 모듈 에러 코드: (status_code, code, message)"""

    # 내부 관리자용 API 에서는 고객사 존재 여부를 숨길 이유가 없으므로 404 로 구분해 응답한다.
    COMPANY_NOT_FOUND = (
        status.HTTP_404_NOT_FOUND,
        "COMPANY_NOT_FOUND",
        "요청한 고객사를 찾을 수 없습니다.",
    )
    COMPANY_INACTIVE = (
        status.HTTP_409_CONFLICT,
        "COMPANY_INACTIVE",
        "비활성화된 고객사입니다. 고객사를 활성화한 뒤 다시 시도해 주십시오.",
    )


class CompanyNotFoundException(BusinessException):
    def __init__(self):
        super().__init__(CompanyErrorCode.COMPANY_NOT_FOUND)


class CompanyInactiveException(BusinessException):
    def __init__(self):
        super().__init__(CompanyErrorCode.COMPANY_INACTIVE)
