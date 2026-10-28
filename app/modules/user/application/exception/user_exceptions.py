from starlette import status

from app.core.exceptions import BaseErrorCode, BusinessException


class UserErrorCode(BaseErrorCode):
    """user 모듈 에러 코드: (status_code, code, message)"""

    # EMAIL
    EMAIL_DUPLICATED = (
        status.HTTP_409_CONFLICT,
        "EMAIL_DUPLICATED",
        "요청 이메일은 이미 사용중입니다. 다른 이메일을 사용해 주십시오.",
    )

    # PASSWORD
    INVALID_PASSWORD_FORMAT = (
        status.HTTP_400_BAD_REQUEST,
        "PASSWORD_INVALID",
        "웹 서버에서 요청한 비밀번호 형식이 올바르지 않습니다.",
    )


class EmailDuplicatedException(BusinessException):
    def __init__(self):
        super().__init__(UserErrorCode.EMAIL_DUPLICATED)


class InvalidPasswordFormatException(BusinessException):
    def __init__(self):
        super().__init__(UserErrorCode.INVALID_PASSWORD_FORMAT)
