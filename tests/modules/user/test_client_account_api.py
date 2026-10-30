import base64
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.core.security.cookies import ACCESS_TOKEN_COOKIE
from app.core.security.jwt import create_access_token
from app.main import app
from app.modules.user.dependencies import get_client_account_service
from app.modules.user.infrastructure.user_enums import UserRoleCode, UserStatus

URL = "/api/v1/admin/users"
COMPANY_ID = uuid.uuid7()


def _body(**overrides) -> dict:
    body = {
        "companyId": str(COMPANY_ID),
        "email": "Lead@Client.com",
        "password": base64.b64encode(b"password123").decode(),
        "roleCode": "CLIENT_LEAD",
        "displayName": "홍길동",
    }
    return {**body, **overrides}


def _access_cookie(role_code: str, company_id: uuid.UUID | None = None) -> dict:
    token = create_access_token(
        user_id=uuid.uuid7(),
        role_code=role_code,
        company_id=company_id,
        config=get_settings().jwt_config,
    )
    return {ACCESS_TOKEN_COOKIE: token}


async def _post(body: dict, cookies: dict | None = None):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", cookies=cookies
    ) as client:
        return await client.post(URL, json=body)


class FakeClientAccountService:
    """서비스는 단위 테스트에서 검증하므로, 라우터 테스트에서는 호출 인자만 기록하고 가짜 사용자를 돌려준다."""

    def __init__(self):
        self.calls: list[dict] = []

    async def create_client_user(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            user_id=uuid.uuid7(),
            company_id=kwargs["company_id"],
            email=kwargs["email"],
            display_name=kwargs["display_name"] or kwargs["email"],
            role_code=kwargs["role_code"],
            status=UserStatus.ACTIVE,
            created_at=datetime(2026, 10, 30, tzinfo=UTC),
            password_hash="$2b$12$should-not-be-exposed",
        )


@pytest.fixture
def fake_service():
    service = FakeClientAccountService()
    app.dependency_overrides[get_client_account_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_client_account_service, None)


async def test_쿠키가_없으면_401을_반환한다(fake_service):
    response = await _post(_body())

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHENTICATED"
    assert fake_service.calls == []


async def test_ADMIN이_아니면_403을_반환한다(fake_service):
    response = await _post(
        _body(), cookies=_access_cookie("CLIENT_LEAD", company_id=COMPANY_ID)
    )

    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
    assert fake_service.calls == []


async def test_Client_역할이_아닌_roleCode는_422를_반환한다(fake_service):
    response = await _post(_body(roleCode="ADMIN"), cookies=_access_cookie("ADMIN"))

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert fake_service.calls == []


async def test_필수_필드가_없으면_422를_반환한다(fake_service):
    body = _body()
    del body["companyId"]

    response = await _post(body, cookies=_access_cookie("ADMIN"))

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_displayName은_50자까지_허용하고_51자는_422를_반환한다(fake_service):
    ok = await _post(_body(displayName="가" * 50), cookies=_access_cookie("ADMIN"))
    assert ok.status_code == 201

    too_long = await _post(
        _body(displayName="가" * 51), cookies=_access_cookie("ADMIN")
    )
    assert too_long.status_code == 422
    assert too_long.json()["code"] == "VALIDATION_ERROR"


async def test_성공하면_201과_camelCase_응답을_반환한다(fake_service):
    response = await _post(_body(), cookies=_access_cookie("ADMIN"))

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {
        "userId",
        "companyId",
        "email",
        "displayName",
        "roleCode",
        "status",
        "createdAt",
    }
    assert body["companyId"] == str(COMPANY_ID)
    assert body["email"] == "lead@client.com"  # 소문자 정규화
    assert body["displayName"] == "홍길동"
    assert body["roleCode"] == "CLIENT_LEAD"
    assert body["status"] == "ACTIVE"

    call = fake_service.calls[0]
    assert call["company_id"] == COMPANY_ID
    assert call["role_code"] == UserRoleCode.CLIENT_LEAD
    assert call["actor"].role_code == "ADMIN"
