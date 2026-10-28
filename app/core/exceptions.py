from enum import Enum

import uuid_utils
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel
from starlette import status
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


def build_error_response(error_code: BaseErrorCode) -> JSONResponse:
    """에러 코드 하나로 공통 형식의 JSON 에러 응답을 만듭니다. 모든 에러 응답은 이 함수를 거칩니다."""
    error_content = ErrorResponse(
        code=error_code.code,
        message=error_code.message,
        request_id=uuid_utils.uuid7().hex,
    ).model_dump()
    return JSONResponse(status_code=error_code.status_code, content=error_content)


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
