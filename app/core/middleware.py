from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import get_settings
from app.core.exceptions import build_internal_error_response


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


def include_middlewares(app: FastAPI) -> None:
    """
    FastAPI 인스턴스에 미들웨어를 일괄 등록합니다.

    add_middleware 는 나중에 호출된 것이 더 바깥에 놓이므로,
    CORS 를 마지막에 추가해 가장 바깥에서 모든 응답에 헤더를 붙이게 합니다.
    """
    settings = get_settings()

    # 안쪽: 처리되지 않은 예외 -> JSON 500
    app.add_middleware(UnhandledExceptionMiddleware)

    # 바깥: CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,  # 쿠키 전송 허용 (프론트는 credentials: "include" 필요)
        allow_methods=["*"],
        allow_headers=["*"],
    )
