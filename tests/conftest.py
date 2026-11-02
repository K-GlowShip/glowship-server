from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings

"""
    테스트 공통 픽스처

    db_session: 실제 DB(.env 의 POSTGRES_*)에 붙는 읽기 전용 세션 (Repository SQL 스모크 테스트용)
      - 트랜잭션을 READ ONLY 로 열고 테스트가 끝나면 롤백하므로 DB 를 바꾸지 않는다.
      - DB 에 접속할 수 없거나 마이그레이션 head(0003) 열이 없으면 건너뛴다 (CI 등).
      - 테스트마다 이벤트 루프가 달라서 앱 전역 엔진 대신 NullPool 엔진을 새로 만든다.
"""

# 0003 에서 추가한 열. 없으면 head 가 아닌 DB 로 보고 건너뛴다.
_HEAD_COLUMN_CHECK = text(
    "SELECT 1 FROM information_schema.columns"
    " WHERE table_schema = 'public' AND table_name = 'analysis_runs'"
    " AND column_name = 'stale_primary_reason'"
)


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
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
        await session.execute(text("SET TRANSACTION READ ONLY"))
        if await session.scalar(_HEAD_COLUMN_CHECK) is None:
            pytest.skip("마이그레이션 head(0003) 가 적용되지 않은 DB 라 건너뜀")
        yield session
    finally:
        await session.close()
        await transaction.rollback()
        await connection.close()
        await engine.dispose()
