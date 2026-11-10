from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core import health
from app.main import app

"""
    GET /health (배포 확인용)
      - DB 가 되면 200 {"status": "ok", "database": "ok"}. DB 가 있어야 하므로 db_session 픽스처로 접속 가능할 때만 돈다.
      - DB 에 붙지 못하면 503 {"status": "error", "database": "error"}. 헬스 모듈의 엔진을 열리지 않는 주소로 바꿔 확인한다.
      - 인증 없이 호출되고 OpenAPI 문서에는 나오지 않는다.
"""

URL = "/health"


async def _get():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get(URL)


async def test_DB가_되면_200과_ok를_돌려준다(db_session):
    response = await _get()

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


async def test_DB에_붙지_못하면_503과_error를_돌려준다(monkeypatch):
    # 포트 1 은 열려 있지 않으므로 접속이 바로 거절된다
    unreachable = create_async_engine(
        "postgresql+asyncpg://nobody:nobody@127.0.0.1:1/nowhere",
        poolclass=NullPool,
        connect_args={"timeout": 1},
    )
    monkeypatch.setattr(health.database, "engine", unreachable)
    try:
        response = await _get()
    finally:
        await unreachable.dispose()

    assert response.status_code == 503
    assert response.json() == {"status": "error", "database": "error"}


async def test_OpenAPI_문서에_나오지_않는다():
    assert URL not in app.openapi()["paths"]
