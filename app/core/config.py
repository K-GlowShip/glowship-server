from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    """
    설정(Settings) 객체 캐싱
      - Settings() 객체는 딱 1번만 만들어지고 그 다음부터 get_settings()를 호출할 때는 기존에 메모리에 저장(캐싱)해 둔 객체를 그대로 반환
    """
    return Settings()
