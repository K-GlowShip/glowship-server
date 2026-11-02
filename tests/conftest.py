from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

# model_registry: 쓰기 테스트가 flush 할 때 모델 간 FK(예: data_sync_runs.company_id → companies)를 해석하려면
# 모든 모델이 메타데이터에 등록돼 있어야 한다 (app/core/database.py 와 같은 이유).
from app.core import model_registry  # noqa: F401
from app.core.config import get_settings

"""
    테스트 공통 픽스처

    db_session: 실제 DB(.env 의 POSTGRES_*)에 붙는 읽기 전용 세션 (Repository SQL 스모크 테스트용)
      - 트랜잭션을 READ ONLY 로 열고 테스트가 끝나면 롤백하므로 DB 를 바꾸지 않는다.
      - DB 에 접속할 수 없거나 마이그레이션 head(0003) 열이 없으면 건너뛴다 (CI 등).
      - 테스트마다 이벤트 루프가 달라서 앱 전역 엔진 대신 NullPool 엔진을 새로 만든다.
    db_write_session: 쓰기 SQL 을 확인하는 세션. 쓰기는 되지만 테스트가 끝나면 롤백하므로 DB 는 그대로다.
"""

# 0003 에서 추가한 열. 없으면 head 가 아닌 DB 로 보고 건너뛴다.
_HEAD_COLUMN_CHECK = text(
    "SELECT 1 FROM information_schema.columns"
    " WHERE table_schema = 'public' AND table_name = 'analysis_runs'"
    " AND column_name = 'stale_primary_reason'"
)


async def _rollback_session(*, read_only: bool) -> AsyncGenerator[AsyncSession]:
    # 접속이 안 되는 환경에서 오래 기다리지 않도록 연결 대기 시간을 짧게 둔다
    engine = create_async_engine(
        get_settings().database_url,
        poolclass=NullPool,
        connect_args={"timeout": 2},
    )
    try:
        connection = await engine.connect()
    except (OSError, SQLAlchemyError) as error:
        await engine.dispose()
        pytest.skip(f"DB 에 접속할 수 없어 건너뜀: {type(error).__name__}")

    transaction = await connection.begin()
    session = AsyncSession(bind=connection)
    try:
        if read_only:
            await session.execute(text("SET TRANSACTION READ ONLY"))
        if await session.scalar(_HEAD_COLUMN_CHECK) is None:
            pytest.skip("마이그레이션 head(0003) 가 적용되지 않은 DB 라 건너뜀")
        yield session
    finally:
        await session.close()
        await transaction.rollback()
        await connection.close()
        await engine.dispose()


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
    async for session in _rollback_session(read_only=True):
        yield session


@pytest.fixture
async def db_write_session() -> AsyncGenerator[AsyncSession]:
    async for session in _rollback_session(read_only=False):
        yield session
