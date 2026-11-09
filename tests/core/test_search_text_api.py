import uuid
from dataclasses import dataclass, field

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security.cookies import ACCESS_TOKEN_COOKIE
from app.core.security.jwt import create_access_token
from app.main import app

"""
    목록 API 검색어 q 의 NUL 문자 (이슈 #49) — DB 없음
      - 11/09 로컬 확인: 여섯 목록 API 모두 q 에 NUL 이 있으면 PostgreSQL 이 바인드 값을 거부해 500 이었다(NUL 없는 q 는 200).
      - 지금은 SearchText 가 요청 검증에서 막아 422 VALIDATION_ERROR 이고, 서비스·DB 까지 가지 않는다.
        FakeSession 은 RLS 컨텍스트 SQL 만 받고, 조회 SQL 이 오면 시험이 실패한다.
"""

PATHS = [
    "/api/v1/decisions",
    "/api/v1/delay-events",
    "/api/v1/inventory",
    "/api/v1/skus",
    "/api/v1/orders",
    "/api/v1/shipments",
]


@dataclass
class FakeSession:
    executed: list = field(default_factory=list)

    async def execute(self, statement, params=None):
        sql = str(statement)
        assert "set_config" in sql, f"검증 전에 조회가 나갔음: {sql[:80]}"
        self.executed.append(sql)


@pytest.fixture
def fake_db():
    session = FakeSession()

    async def _get_db():
        yield session

    app.dependency_overrides[get_db] = _get_db
    yield session
    app.dependency_overrides.pop(get_db, None)


@pytest.mark.parametrize("path", PATHS)
async def test_nul_in_search_text_is_422_not_500(fake_db, path):
    token = create_access_token(
        user_id=uuid.uuid7(),
        role_code="CLIENT_LEAD",
        company_id=uuid.uuid7(),
        config=get_settings().jwt_config,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={ACCESS_TOKEN_COOKIE: token},
    ) as client:
        response = await client.get(path, params={"q": "SKU\x00001"})

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
