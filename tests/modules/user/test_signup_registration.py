import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.main import include_dev_routers

"""
    운영자 회원가입은 local 에서만 등록한다 (이슈 #78, #99)
      - 인증 없이 열려 있어 공개 서버(dev·prod)에 두면 누구나 운영자 계정을 만들 수 있다(명세 D5).
      - APP_ENV 를 빠뜨리면 local 로 뜨지 않고 서버가 뜨지 않는다(기본값 없음).
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


@pytest.mark.parametrize("app_env", ["dev", "prod"])
def test_public_servers_do_not_register_signup_or_dev_routes(app_env):
    routes = _routes(app_env)

    assert SIGNUP not in routes
    assert AGENT_DRY_RUN not in routes


def test_local_registers_signup_on_same_path():
    routes = _routes("local")

    assert SIGNUP in routes
    assert AGENT_DRY_RUN in routes


def test_missing_app_env_fails_instead_of_falling_back_to_local(monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)

    with pytest.raises(ValidationError) as error:
        Settings(
            _env_file=None,
            postgres_host="h",
            postgres_port=5432,
            postgres_user="u",
            postgres_password="p",
            postgres_db="d",
            jwt_secret_key="x" * 32,
        )

    assert [e["loc"] for e in error.value.errors()] == [("app_env",)]
