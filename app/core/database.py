from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# model_registry: 모델 간 FK(예: users.company_id → companies)를 flush 시점에 해석하려면
# 세션을 쓰기 전에 모든 ORM 모델이 Base.metadata 에 등록되어 있어야 한다.
from app.core import model_registry  # noqa: F401
from app.core.config import get_settings

"""
    DB 커넥션 연결, 세션 설정 및 관리
"""

# Database Connection
engine = create_async_engine(
    get_settings().database_url,
    echo=True,  # SQLAlchemy가 실행하는 모든 SQL 쿼리를 콘솔(로그)에 출력
    future=True,  # SQLAlchemy 2.0 스타일의 API(새로운 방식)를 강제로 사용
)

# Init Session Factory
SessionLocal = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,  # 객체의 데이터를 만료(Expire)시키지 않고 그대로 유지
    autocommit=False,  # 자동 커밋 기능을 비활성화
    autoflush=False,  # 자동으로 플러시(Flush)되는 기능을 비활성화 (asyncpg 드라이버 사용하기 위한 필수 설정 -> 비동기이기 때문)
)


# 워커 DB 역할 이름 (마이그레이션 0001·v6.2 DDL 과 같음)
WORKER_DB_ROLE = "glowship_v4_worker"


def create_worker_engine(url: str, **kwargs: Any) -> AsyncEngine:
    """
    수집 적재 전용 엔진. 연결할 때마다 DB 역할을 워커 역할로 바꾼다(asyncpg server_settings role = SET ROLE).
      - RLS 정책은 app_context_allowed(current_user) 로 역할 이름을 직접 비교한다(0001).
        워커 역할에 속한 로그인 계정으로 그냥 붙으면 current_user 가 로그인 계정 이름이라 모든 행이 숨겨지고
        INSERT 는 RLS 위반이 된다(PR #40 리뷰). 연결 단계에서 고정하면 트랜잭션마다 SET ROLE 을 빠뜨릴 일이 없다.
      - 로그인 계정이 워커 역할에 속하지 않으면 연결이 실패한다(잘못 설정한 계정이 조용히 붙지 않음).
    """
    connect_args = {
        **kwargs.pop("connect_args", {}),
        "server_settings": {"role": WORKER_DB_ROLE},
    }
    return create_async_engine(url, connect_args=connect_args, **kwargs)


# 수집 적재 전용 연결 (워커 역할, GLOW-13). 워커 계정을 따로 두지 않으면 앱 연결을 그대로 쓴다.
worker_engine = (
    create_worker_engine(get_settings().worker_database_url, echo=True, future=True)
    if get_settings().uses_separate_worker_account
    else engine
)

WorkerSessionLocal = async_sessionmaker(
    bind=worker_engine, expire_on_commit=False, autocommit=False, autoflush=False
)


@asynccontextmanager
async def open_worker_session() -> AsyncIterator[AsyncSession]:
    """
    수집 적재 전용 세션 (요청 세션 get_db 와 따로)
      - 수집 서비스(DataIngestService)가 RUNNING 기록·적용·FAILED 기록을 직접 commit·rollback 하므로
        요청 세션을 공유하지 않고 이 세션을 넘깁니다. 여기서는 commit 하지 않고 닫기만 합니다.
      - 요청 응답(202) 뒤 백그라운드에서 열어 씁니다(API 25).
    """
    async with WorkerSessionLocal() as session:
        yield session


async def get_db() -> AsyncGenerator[AsyncSession]:
    """요청 단위 세션: 정상 종료 시 commit, 예외 발생 시 rollback 후 재전파, 종료 시 자동 close"""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
