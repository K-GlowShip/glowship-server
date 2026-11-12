from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.security.jwt import JwtConfig

# 앱 DB 역할 이름 (마이그레이션 0001·v6.2 DDL 과 같음). POSTGRES_ROLE 이 받을 수 있는 유일한 값
APP_DB_ROLE = "glowship_v4_app"
# AI 설명 기본 모델: 엔진의 결정적 mock (외부 호출 없음). 실제 모델은 "제공자/모델 ID" 로 적는다
MOCK_EXPLANATION_MODEL = "mock/mock-template-v1"


class Settings(BaseSettings):
    """env 파일에 작성되어진 정적 파일 관리"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: Literal["local", "dev", "prod"] = "local"
    # 앱 로그 수준 (app/core/log_config.py). 앱 로거("app" 아래)만 이 수준이고 라이브러리 로그는 WARNING 그대로다.
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # 쿠키(credentials)를 쓰는 CORS 에서는 "*" 를 쓸 수 없으므로 허용 오리진을 명시한다.
    # .env 예시: CORS_ORIGINS=["http://localhost:3000","http://localhost:5173"]
    cors_origins: list[str] = []

    postgres_host: str
    postgres_port: int
    postgres_user: str
    postgres_password: str
    postgres_db: str

    # 수집 적재 전용 워커 DB 계정 (GLOW-13, 수집 서비스 ingest_service "실행 전제")
    # 수집은 워커 역할(glowship_v4_worker) 권한으로 돌아야 한다. 운영은 그 역할에 속한 로그인 계정을 넣고,
    # 연결할 때 역할을 워커 역할로 바꾼다(app/core/database.py create_worker_engine, RLS 가 역할 이름을 비교).
    # 둘 다 비우면 위 POSTGRES_USER·PASSWORD 로 붙는다 (로컬 superuser 개발 DB 는 지금처럼 동작).
    # 호스트·포트·DB 이름은 앱 연결과 같다.
    worker_postgres_user: str | None = None
    worker_postgres_password: str | None = None

    # 앱 연결의 DB 역할 고정 (이슈 #41). 운영은 앱 역할(glowship_v4_app)에 속한 로그인 계정으로 붙고 이 값을
    # glowship_v4_app 으로 넣는다. 연결할 때 역할을 바꾼다(app/core/database.py create_app_engine, RLS 가 역할 이름을 비교).
    # 비우면 지금처럼 로그인 계정 그대로 붙는다(로컬 superuser 개발·데모 DB). 앱 역할 말고 다른 값은 기동이 실패한다.
    # 넣으면 워커 계정(WORKER_POSTGRES_USER·PASSWORD)도 있어야 한다: 없으면 수집이 앱 역할로 돌아 행이 모두 숨겨진다.
    postgres_role: str | None = None

    # JWT
    # HS256 은 RFC 7518 3.2 에 따라 32바이트 이상 키를 권장한다. 짧으면 기동 시점에 실패시킨다.
    # 생성 예: python -c "import secrets; print(secrets.token_urlsafe(48))"
    jwt_secret_key: str = Field(min_length=32)
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 480  # 8시간. 개발 단계 프론트와 합의한 값
    refresh_token_expire_days: int = 7

    # Cookie
    # Secure 쿠키는 HTTPS 에서만 전송된다. 미설정(None)이면 local 환경만 False, 그 외는 True 로 둔다.
    cookie_secure: bool | None = None

    # Data sync
    # 수집 멈춤 판정 배수. 마지막 수집 시작이 갱신 주기 × 배수보다 오래되면 동기화 안 됨으로 본다.
    # 0 이면 판정하지 않는다 (합성데이터처럼 오래된 이력으로 화면을 확인할 때).
    sync_stale_multiplier: int = Field(default=2, ge=0)

    # 가상 시각 (GLOW-13·49 데모, 2026-11-04 결정). 데이터가 2026-06-30 까지이고 마지막 수집이 07-01 이라 데모의 "지금"은 7월 초다.
    # 값이 있으면 수동 수집(API 25)은 서버의 지금 시각이 아니라 "마지막 적재 SHIPMENT 수집 날짜 + 1일 00:00Z"(처음은 이 값)로
    # 수집하고, 수집 현황의 동기화 판정은 수집 멈춤(주기 × 배수)을 보지 않는다 (virtual_clock.py, data_sync_status_service.py).
    # 비우면(VIRTUAL_CLOCK_START=) 서버의 지금 시각을 쓴다. 실제 원천을 붙이는 환경(운영 배포)에서는 반드시 비운다.
    virtual_clock_start: datetime | None = datetime(2026, 7, 2, tzinfo=UTC)

    # 가상 TMS 시나리오 파일 (GLOW-49, infrastructure/virtual_tms.py). 수동 수집(API 25) SHIPMENT 가 이 파일에서 응답을 읽는다.
    # 비우면 원천 응답이 없는 것으로 보고 0건 수집을 남긴다. 요청마다 다시 읽으므로 파일을 고쳐도 재시작이 필요 없다.
    virtual_tms_scenario_path: Path | None = None

    # 가상 ERP·WMS 시나리오 파일 (2026-11-09 결정, infrastructure/virtual_sources.py). 수동 동기화(API 25)의 주문·재고 응답.
    # 가상 TMS 파일과 합쳐 읽는다(census extend_days 결과, 예: GLOW-13_가상ERP_WMS_0702-0712_A_BEAUTY.json). 비우면 주문·재고 0건 수집.
    virtual_erp_wms_scenario_path: Path | None = None

    # Analysis engine
    # 데이터 팀 분석 엔진 폴더(glowship_engine·synth2025_v5 의 부모). 엔진 코드는 저장소에 넣지 않고 이 경로에서
    # 해시를 대조해 불러온다(docs/analysis/engine-local-path.md). 비어 있으면 엔진이 필요한 기능·시험이 동작하지 않는다.
    glowship_engine_dir: Path | None = None
    # 엔진 저장소 캐시 항목 수 상한 (이슈 #60, docs/analysis/engine-store-cache.md). 열쇠 = (고객사, 입력 순번).
    # 저장소 하나가 0.3~0.5GB(2026 H1 고객사 2곳 실측)라 기본값은 고객사 수(2)로 둔다. 고객사가 늘면 고객사 수만큼 올린다.
    # 0 이면 캐시를 끄고 실행마다 저장소를 새로 읽는다.
    engine_store_cache_max_entries: int = Field(default=2, ge=0)

    # Analysis auto start (GLOW-128, 이슈 #51, docs/analysis/auto-start.md)
    # 켜면 지연 감지(AUTO)·재분석(API 20)으로 만든 실행을 서버 안에서 바로 분석하고 저장한다. 서버 시작 때 남은 실행을 정리한다.
    # 기본은 꺼짐: 실제 실행 묶음(계약 도구 12개)은 만들 수 있지만, 실제 묶음 끝까지 시험(평가 시나리오 정답 대조)과
    # NFR-001 측정 전이다. 켜는 조건은 docs/analysis/auto-start.md 7절(사용자 결정 11-09·11-10).
    # 꺼져 있으면 지금처럼 실행이 DETECTED 로 남는다.
    analysis_auto_start_enabled: bool = False
    # 한 서버 프로세스가 동시에 돌리는 분석 실행 수. 넘치면 DETECTED 그대로 기다린다(docs/analysis/auto-start.md 2절).
    analysis_max_concurrent_runs: int = Field(default=4, ge=1)
    # 서버 종료 때 돌던 분석을 기다리는 시간(초). 넘으면 취소하고, 그 실행은 다음 시작 때 FAILED 로 정리된다.
    analysis_shutdown_grace_seconds: float = Field(default=10.0, ge=0)
    # 분석 실행 하나의 시간 제한(초, 이슈 #123, docs/analysis/auto-start.md 2절). dev 사양 측정 전 잠정값.
    # 분석 = 시작 전이부터 그래프 끝까지(자리를 기다린 시간은 뺌). 넘으면 FAILED(RUN_TIME_LIMIT_EXCEEDED).
    # 저장 = 결과 저장(저장 직후 재판정의 판정 서명 재계산 포함). 넘으면 재계산 없이 다시 저장(걸린 변경이 있으면 STALE).
    analysis_run_time_limit_seconds: float = Field(default=120.0, gt=0)
    analysis_save_time_limit_seconds: float = Field(default=60.0, gt=0)
    # 실행 추적 내부 로그 폴더 (이슈 #51, docs/agent-flow/agent-tracing.md 3절). 실행 사건(노드·도구·설명 시도, 마스킹됨)을
    # <폴더>/agent_trace_outbox/<고객사>/<UTC 날짜>.jsonl 에 한 줄씩 남긴다(agentflow 8.1 "외부 전송 불허 시").
    # 비우면(기본) 추적하지 않는다. 배포 컨테이너는 /app 아래에 쓸 수 없어, 쓸 수 있는 폴더(볼륨)를 정한 뒤 넣는다.
    agent_trace_dir: Path | None = None

    # Language model (GLOW-150, docs/agent-flow/explanation.md 8절)
    # AI 설명을 만드는 모델. "제공자/모델 ID" 한 줄이다(예: openai/<모델 ID>). 기본은 엔진 mock 이라 외부 호출이 없다.
    # 실제 모델은 엔진 등록부에 그 모델 설정이 있고 제공자 키가 있어야 한다. 아니면 서버가 시작 단계에서 멈춘다.
    explanation_model: str = MOCK_EXPLANATION_MODEL
    # 모델 호출 한 번의 시간 제한(초)과 설명 한 건(엔진 3시도 합)의 총 시간 예산(초). 모델별 측정 뒤 조정한다.
    explanation_model_timeout_seconds: float = Field(default=12.0, gt=0)
    explanation_total_budget_seconds: float = Field(default=20.0, gt=0)
    # 제공자 API 키. 기능마다 따로 두지 않고 제공자마다 하나다(모델은 기능별 설정). 고른 제공자의 키만 있으면 된다.
    openai_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None

    @field_validator("glowship_engine_dir", "agent_trace_dir", mode="before")
    @classmethod
    def _blank_engine_dir_is_unset(cls, value: object) -> object:
        # `GLOWSHIP_ENGINE_DIR=` 처럼 값을 비워 두면 Path("") 가 현재 폴더(".")가 되어 설정된 것으로 보인다.
        # 빈 값·공백은 설정하지 않은 것(None)으로 본다.
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("explanation_model", mode="before")
    @classmethod
    def _blank_explanation_model_is_mock(cls, value: object) -> object:
        # `EXPLANATION_MODEL=` 처럼 비워 두면 기본(mock)으로 본다.
        if isinstance(value, str):
            return value.strip() or MOCK_EXPLANATION_MODEL
        return value

    @field_validator("explanation_model")
    @classmethod
    def _explanation_model_names_provider(cls, value: str) -> str:
        provider, _, model_id = value.partition("/")
        if not provider or not model_id or value != value.strip():
            raise ValueError(
                'EXPLANATION_MODEL 은 "제공자/모델 ID" 여야 합니다 (예: mock/mock-template-v1)'
            )
        return value

    @field_validator(
        "openai_api_key", "gemini_api_key", "anthropic_api_key", mode="before"
    )
    @classmethod
    def _blank_api_key_is_unset(cls, value: object) -> object:
        # `OPENAI_API_KEY=` 처럼 비워 두면 키가 없는 것으로 본다(빈 키로 호출하지 않게).
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("agent_trace_dir")
    @classmethod
    def _trace_dir_is_absolute(cls, value: Path | None) -> Path | None:
        # 엔진 경로(engine_loader)와 같게 ~ 와 상대 경로를 절대 경로로 바꾼다(상대 경로는 서버를 띄운 폴더 기준).
        # 그대로 두면 "~/trace" 가 실행 폴더 아래 "~" 폴더가 된다.
        return None if value is None else value.expanduser().resolve()

    @field_validator(
        "virtual_tms_scenario_path", "virtual_erp_wms_scenario_path", mode="before"
    )
    @classmethod
    def _blank_scenario_path_is_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("virtual_clock_start", mode="before")
    @classmethod
    def _blank_virtual_clock_is_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("virtual_clock_start")
    @classmethod
    def _virtual_clock_needs_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError(
                "VIRTUAL_CLOCK_START 는 시간대가 있는 시각이어야 합니다 (예: 2026-07-02T00:00:00Z)"
            )
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _log_level_upper(cls, value: object) -> object:
        # LOG_LEVEL=info 처럼 소문자로 넣어도 받는다
        return value.upper() if isinstance(value, str) else value

    @field_validator("postgres_role", mode="before")
    @classmethod
    def _blank_role_is_unset(cls, value: object) -> object:
        # `POSTGRES_ROLE=` 처럼 비워 두면 역할을 고정하지 않는다.
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("postgres_role")
    @classmethod
    def _only_app_role(cls, value: str | None) -> str | None:
        if value is not None and value != APP_DB_ROLE:
            raise ValueError(
                f"POSTGRES_ROLE 는 비우거나 {APP_DB_ROLE} 이어야 합니다 (받은 값: {value})"
            )
        return value

    @field_validator("worker_postgres_user", "worker_postgres_password", mode="before")
    @classmethod
    def _blank_worker_credential_is_unset(cls, value: object) -> object:
        # `WORKER_POSTGRES_USER=` 처럼 비워 두면 설정하지 않은 것(None)으로 본다.
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _worker_credentials_come_together(self) -> Settings:
        # 계정만 있고 비밀번호가 없는(또는 그 반대) 설정은 운영에서 조용히 앱 계정으로 붙지 않도록 기동 시점에 실패시킨다.
        if (self.worker_postgres_user is None) != (
            self.worker_postgres_password is None
        ):
            raise ValueError(
                "WORKER_POSTGRES_USER 와 WORKER_POSTGRES_PASSWORD 는 함께 넣거나 함께 비워야 합니다."
            )
        # 앱 연결을 앱 역할로 고정하면서 워커 계정이 없으면 수집 세션이 앱 연결을 그대로 써 앱 역할로 돈다.
        # 앱 역할은 사용자 컨텍스트를 요구하므로 수집 조회가 비고 INSERT 가 실패한다(docs/data-sync/ingest.md).
        if self.postgres_role is not None and self.worker_postgres_user is None:
            raise ValueError(
                "POSTGRES_ROLE 을 넣으면 WORKER_POSTGRES_USER·PASSWORD 도 넣어야 합니다 (수집은 워커 역할로 돈다)."
            )
        return self

    def _postgres_url(self, user: str, password: str) -> str:
        return (
            f"postgresql+asyncpg://{user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def database_url(self) -> str:
        return self._postgres_url(self.postgres_user, self.postgres_password)

    @property
    def uses_separate_worker_account(self) -> bool:
        return self.worker_postgres_user is not None

    @property
    def worker_database_url(self) -> str:
        """수집 적재 전용 연결. 워커 계정이 없으면 앱 연결과 같다"""
        if self.worker_postgres_user is None or self.worker_postgres_password is None:
            return self.database_url
        return self._postgres_url(
            self.worker_postgres_user, self.worker_postgres_password
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
