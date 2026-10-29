from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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


async def get_db() -> AsyncGenerator[AsyncSession]:
    """요청 단위 세션: 정상 종료 시 commit, 예외 발생 시 rollback 후 재전파, 종료 시 자동 close"""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
