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
        "INVALID_PASSWORD_FORMAT",
        "웹 서버에서 요청한 비밀번호 형식이 올바르지 않습니다.",
    )

    # LOGIN
    # 이메일 없음과 비밀번호 불일치를 같은 코드·메시지로 응답해 계정 존재 여부를 노출하지 않는다.
    INVALID_CREDENTIALS = (
        status.HTTP_401_UNAUTHORIZED,
        "INVALID_CREDENTIALS",
        "이메일 또는 비밀번호가 올바르지 않습니다.",
    )
    ACCOUNT_DISABLED = (
        status.HTTP_403_FORBIDDEN,
        "ACCOUNT_DISABLED",
        "비활성화된 계정입니다. 관리자에게 문의해 주십시오.",
    )


class EmailDuplicatedException(BusinessException):
    def __init__(self):
        super().__init__(UserErrorCode.EMAIL_DUPLICATED)


class InvalidPasswordFormatException(BusinessException):
    def __init__(self):
        super().__init__(UserErrorCode.INVALID_PASSWORD_FORMAT)


class InvalidCredentialsException(BusinessException):
    def __init__(self):
        super().__init__(UserErrorCode.INVALID_CREDENTIALS)


class AccountDisabledException(BusinessException):
    def __init__(self):
        super().__init__(UserErrorCode.ACCOUNT_DISABLED)
