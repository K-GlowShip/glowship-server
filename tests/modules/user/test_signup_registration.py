import pytest
from fastapi import FastAPI

from app.core.config import get_settings
from app.main import include_dev_routers

"""
    운영자 회원가입은 운영(app_env=prod)에서 등록하지 않는다 (이슈 #78)
      - 인증 없이 열려 있어 운영에 두면 누구나 운영자 계정을 만들 수 있다(명세 D5).
      - 경로(/api/v1/auth/signup)는 그대로 두고 등록만 환경에 따라 나눈다. 같은 블록의 에이전트 확인용 라우터도 함께 본다.
      - DB 없이 등록된 경로(OpenAPI)만 본다.
"""

SIGNUP = ("POST", "/api/v1/auth/signup")
AGENT_DRY_RUN = ("POST", "/api/v1/dev/agent/dry-run")


def _routes(app_env: str) -> set[tuple[str, str]]:
    app = FastAPI()
    include_dev_routers(app, get_settings().model_copy(update={"app_env": app_env}))
    # 지금 FastAPI 판(0.162)은 include_router 한 라우트를 app.routes 에 펼치지 않아 OpenAPI 경로로 본다
    return {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        for method in operations
    }


def test_prod_does_not_register_signup_or_dev_routes():
    routes = _routes("prod")

    assert SIGNUP not in routes
    assert AGENT_DRY_RUN not in routes


@pytest.mark.parametrize("app_env", ["local", "dev"])
def test_non_prod_registers_signup_on_same_path(app_env):
    routes = _routes(app_env)

    assert SIGNUP in routes
    assert AGENT_DRY_RUN in routes
