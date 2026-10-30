from datetime import timedelta
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.security.jwt import JwtConfig


class Settings(BaseSettings):
    """env 파일에 작성되어진 정적 파일 관리"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: Literal["local", "dev", "prod"] = "local"

    # 쿠키(credentials)를 쓰는 CORS 에서는 "*" 를 쓸 수 없으므로 허용 오리진을 명시한다.
    # .env 예시: CORS_ORIGINS=["http://localhost:3000","http://localhost:5173"]
    cors_origins: list[str] = []

    postgres_host: str
    postgres_port: int
    postgres_user: str
    postgres_password: str
    postgres_db: str

    # JWT
    # HS256 은 RFC 7518 3.2 에 따라 32바이트 이상 키를 권장한다. 짧으면 기동 시점에 실패시킨다.
    # 생성 예: python -c "import secrets; print(secrets.token_urlsafe(48))"
    jwt_secret_key: str = Field(min_length=32)
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7

    # Cookie
    # Secure 쿠키는 HTTPS 에서만 전송된다. 미설정(None)이면 local 환경만 False, 그 외는 True 로 둔다.
    cookie_secure: bool | None = None

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def jwt_config(self) -> JwtConfig:
        """토큰 발급 함수에 넘기는 서명·만료 설정"""
        return JwtConfig(
            secret_key=self.jwt_secret_key,
            algorithm=self.jwt_algorithm,
            access_token_expires=timedelta(minutes=self.access_token_expire_minutes),
            refresh_token_expires=timedelta(days=self.refresh_token_expire_days),
        )

    @property
    def cookie_secure_enabled(self) -> bool:
        """COOKIE_SECURE 가 명시되면 그 값을, 아니면 app_env 로 결정한다."""
        if self.cookie_secure is not None:
            return self.cookie_secure
        return self.app_env != "local"


@lru_cache
def get_settings() -> Settings:
    """
    설정(Settings) 객체 캐싱
      - Settings() 객체는 딱 1번만 만들어지고 그 다음부터 get_settings()를 호출할 때는 기존에 메모리에 저장(캐싱)해 둔 객체를 그대로 반환
    """
    return Settings()
