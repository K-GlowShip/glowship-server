import uuid
from enum import Enum

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel
from starlette import status
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse


class ErrorResponse(BaseModel):
    code: str
    message: str
    request_id: str


class BaseErrorCode(Enum):
    """
    에러 코드 Enum 의 공통 베이스.

    각 모듈은 이 클래스를 상속해 멤버를 (status_code, code, message) 튜플로 선언합니다.
        class UserErrorCode(BaseErrorCode):
            EMAIL_DUPLICATED = (status.HTTP_409_CONFLICT, "EMAIL_DUPLICATED", "이미 사용중인 이메일입니다.")
    Enum 은 튜플 값을 __init__ 인자로 풀어 넘기므로 멤버가 세 속성을 갖게 됩니다.
    """

    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message


class CommonErrorCode(BaseErrorCode):
    """특정 도메인에 속하지 않는 공통 에러 코드"""

    VALIDATION_ERROR = (
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "VALIDATION_ERROR",
        "요청 값이 올바르지 않습니다.",
    )
    INTERNAL_SERVER_ERROR = (
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        "INTERNAL_SERVER_ERROR",
        "서버 내부 오류가 발생했습니다.",
    )
    NOT_FOUND = (
        status.HTTP_404_NOT_FOUND,
        "NOT_FOUND",
        "요청한 리소스를 찾을 수 없습니다.",
    )
    METHOD_NOT_ALLOWED = (
        status.HTTP_405_METHOD_NOT_ALLOWED,
        "METHOD_NOT_ALLOWED",
        "허용되지 않은 HTTP 메서드입니다.",
    )
    # 인증·인가 (모든 모듈의 보호된 API 가 공통으로 사용)
    UNAUTHENTICATED = (
        status.HTTP_401_UNAUTHORIZED,
        "UNAUTHENTICATED",
        "로그인이 필요합니다.",
    )
    FORBIDDEN = (
        status.HTTP_403_FORBIDDEN,
        "FORBIDDEN",
        "접근 권한이 없습니다.",
    )


# Starlette 라우터가 던지는 HTTPException 의 상태 코드를 공통 에러 코드로 잇는 매핑
_HTTP_STATUS_TO_ERROR_CODE: dict[int, CommonErrorCode] = {
    status.HTTP_404_NOT_FOUND: CommonErrorCode.NOT_FOUND,
    status.HTTP_405_METHOD_NOT_ALLOWED: CommonErrorCode.METHOD_NOT_ALLOWED,
}


def _build_response(status_code: int, code: str, message: str) -> JSONResponse:
    error_content = ErrorResponse(
        code=code,
        message=message,
        request_id=uuid.uuid7().hex,
    ).model_dump()
    return JSONResponse(status_code=status_code, content=error_content)


def build_error_response(error_code: BaseErrorCode) -> JSONResponse:
    """에러 코드 하나로 공통 형식의 JSON 에러 응답을 만듭니다. 모든 에러 응답은 이 함수를 거칩니다."""
    return _build_response(error_code.status_code, error_code.code, error_code.message)


def build_internal_error_response() -> JSONResponse:
    """처리되지 않은 예외를 클라이언트에 노출하지 않고 공통 형식의 500 응답으로 변환한다."""
    return build_error_response(CommonErrorCode.INTERNAL_SERVER_ERROR)


class BusinessException(Exception):
    """
    서비스 계층에서 던지는 비즈니스 오류.
    """

    def __init__(self, error_code: BaseErrorCode):
        self.error_code = error_code
        super().__init__(error_code.message)


class UnauthenticatedException(BusinessException):
    """access 토큰이 없거나 신뢰할 수 없을 때 (401). 프론트는 이 코드를 받으면 재발급을 시도한다."""

    def __init__(self):
        super().__init__(CommonErrorCode.UNAUTHENTICATED)


class ForbiddenException(BusinessException):
    """로그인은 했지만 역할이 허용되지 않을 때 (403)"""

    def __init__(self):
        super().__init__(CommonErrorCode.FORBIDDEN)


def include_exception_handlers(app: FastAPI) -> None:
    """FastAPI 인스턴스에 핸들러들을 일괄 등록해주는 함수입니다."""

    @app.exception_handler(BusinessException)
    async def business_exception_handler(request: Request, exc: BusinessException):
        return build_error_response(exc.error_code)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ):
        """FastAPI 의 기본 422 응답({"detail": [...]})을 프로젝트 공통 ErrorResponse 형식으로 변환합니다."""
        return build_error_response(CommonErrorCode.VALIDATION_ERROR)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        """
        없는 경로(404)·잘못된 메서드(405) 등 Starlette/FastAPI 가 던지는 HTTPException 을
        기본 {"detail": ...} 대신 공통 ErrorResponse 형식으로 변환합니다.

        매핑에 없는 상태 코드는 상태 코드를 그대로 유지하고 code 만 일반값(HTTP_ERROR)으로 둡니다.
        (서비스 계층은 BusinessException 만 던지는 것이 규칙이므로 이 경로로 오는 일은 거의 없습니다.)
        """
        error_code = _HTTP_STATUS_TO_ERROR_CODE.get(exc.status_code)
        if error_code is not None:
            return build_error_response(error_code)
        return _build_response(exc.status_code, "HTTP_ERROR", str(exc.detail))
