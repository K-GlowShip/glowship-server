from datetime import timedelta

from app.core.config import Settings

"""
    토큰 수명 기본값 (GLOW-158, API 명세 D4)
      - access 30분·refresh 7일. 다른 시험은 쿠키 Max-Age 를 설정값과 대조할 뿐이라, 기본값을 되돌려도 통과한다.
        기본값 자체를 여기서 고정한다. 바꾸려면 명세 D4·4.1 과 docs/auth/login.md 를 함께 고친다.
      - 환경 변수(ACCESS_TOKEN_EXPIRE_MINUTES)를 주면 그 값이 기본값보다 우선한다(서버 env 에 남아 있으면 기본값이 적용되지 않음).
"""

REQUIRED = {
    "app_env": "local",
    "postgres_host": "db",
    "postgres_port": 5432,
    "postgres_user": "app_login",
    "postgres_password": "app_pw",
    "postgres_db": "glowship",
    "jwt_secret_key": "x" * 32,
}


def test_default_token_lifetimes_are_30_minutes_and_7_days(monkeypatch):
    monkeypatch.delenv("ACCESS_TOKEN_EXPIRE_MINUTES", raising=False)
    monkeypatch.delenv("REFRESH_TOKEN_EXPIRE_DAYS", raising=False)

    config = Settings(_env_file=None, **REQUIRED).jwt_config

    assert config.access_token_expires == timedelta(minutes=30)
    assert config.access_token_expires.total_seconds() == 1800
    assert config.refresh_token_expires == timedelta(days=7)


def test_env_value_overrides_default_access_lifetime(monkeypatch):
    monkeypatch.setenv("ACCESS_TOKEN_EXPIRE_MINUTES", "480")

    config = Settings(_env_file=None, **REQUIRED).jwt_config

    assert config.access_token_expires.total_seconds() == 28800
