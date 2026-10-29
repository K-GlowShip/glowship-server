from collections.abc import Iterable

from fastapi import FastAPI
from starlette.datastructures import Headers
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import get_settings
from app.core.exceptions import (
    CommonErrorCode,
    build_error_response,
    build_internal_error_response,
)


class UnhandledExceptionMiddleware:
    """
    핸들러/의존성에서 처리되지 않은 예외를 공통 JSON 500 응답으로 변환합니다.

    Starlette 의 기본 500 처리기는 모든 미들웨어보다 바깥에 있어서 그 응답에는 CORS 헤더가
    붙지 않습니다. 이 미들웨어를 CORS 미들웨어보다 "안쪽" 에 두면, 예외가 여기서 응답으로
    바뀐 뒤 CORS 미들웨어를 통과하므로 500 응답에도 CORS 헤더가 붙습니다.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            if response_started:
                # 이미 응답 전송이 시작된 뒤에는 새 응답을 보낼 수 없으므로 그대로 전파
                raise
            response = build_internal_error_response()
            await response(scope, receive, send)


# 서버 상태를 바꾸는 메서드. 안전한 메서드(GET·HEAD·OPTIONS)는 검사하지 않는다.
# 브라우저는 같은 오리진의 GET 에 Origin 헤더를 붙이지 않는 경우가 있어, 전 메서드에 걸면 Swagger 의 GET 이 깨진다.
_STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class OriginCheckMiddleware:
    """
    CSRF 방어: 상태를 바꾸는 요청의 Origin 헤더가 허용 목록에 있는지 확인합니다.

    두 토큰을 모두 쿠키에 두면 브라우저가 쿠키를 자동으로 보내므로, 다른 사이트에서 띄운
    요청도 로그인된 것처럼 처리될 수 있다. SameSite=Lax 가 1차 방어이고, 이 미들웨어가
    서버 쪽에서 출처를 직접 확인하는 2차 방어다.

    - 허용 목록: 프론트 오리진과 API 자신의 오리진(Swagger). CORS 와 같은 목록을 쓴다.
    - Origin 헤더 없음: local 에서만 통과시킨다(curl 등). 그 외 환경은 403.
    - 거절 응답은 공통 에러 형식의 403 INVALID_ORIGIN. 허용되지 않은 오리진이므로
      CORS 미들웨어가 허용 헤더를 붙이지 않는 것이 정상이다.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        allowed_origins: Iterable[str],
        allow_missing_origin: bool,
    ) -> None:
        self.app = app
        self.allowed_origins = frozenset(allowed_origins)
        self.allow_missing_origin = allow_missing_origin

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in _STATE_CHANGING_METHODS:
            await self.app(scope, receive, send)
            return

        origin = Headers(scope=scope).get("origin")
        if origin is None:
            allowed = self.allow_missing_origin
        else:
            # "null"(샌드박스 iframe, 리다이렉트 등)은 목록에 없으므로 자연히 거절된다.
            allowed = origin in self.allowed_origins

        if not allowed:
            response = build_error_response(CommonErrorCode.INVALID_ORIGIN)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


def include_middlewares(app: FastAPI) -> None:
    """
    FastAPI 인스턴스에 미들웨어를 일괄 등록합니다.

    add_middleware 는 나중에 호출된 것이 더 바깥에 놓이므로,
    CORS 를 마지막에 추가해 가장 바깥에서 모든 응답에 헤더를 붙이게 합니다.
    """
    settings = get_settings()

    # 안쪽: 처리되지 않은 예외 -> JSON 500
    app.add_middleware(UnhandledExceptionMiddleware)

    # 가운데: 출처 검사 (CSRF 방어). CORS 와 같은 허용 목록을 쓰며, Swagger 를 쓰려면
    # API 자신의 오리진(예: http://localhost:8000)도 CORS_ORIGINS 에 넣어야 한다.
    # CORS 보다 안쪽에 두어, 허용된 오리진에서 발생한 401·403·500 응답에는 CORS 헤더가 붙고
    # 허용되지 않은 오리진의 403 INVALID_ORIGIN 에는 붙지 않게 한다.
    app.add_middleware(
        OriginCheckMiddleware,
        allowed_origins=settings.cors_origins,
        allow_missing_origin=settings.app_env == "local",  # curl 등 Origin 없는 요청은 로컬만 허용
    )

    # 바깥: CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,  # 쿠키 전송 허용 (프론트는 credentials: "include" 필요)
        allow_methods=["*"],
        allow_headers=["*"],
    )
