import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.core.exceptions import ForbiddenException
from app.core.security.cookies import ACCESS_TOKEN_COOKIE
from app.core.security.jwt import create_access_token
from app.main import app
from app.modules.user.dependencies import get_client_account_service
from app.modules.user.infrastructure.user_enums import UserRoleCode, UserStatus

URL = "/api/v1/admin/users"
COMPANY_ID = uuid.uuid7()
ADMIN_ID = uuid.uuid7()
LEAD_ID = uuid.uuid7()
MEMBER_ID = uuid.uuid7()


def _access_cookie(role_code: str, company_id: uuid.UUID | None = None) -> dict:
    token = create_access_token(
        user_id=ADMIN_ID,
        role_code=role_code,
        company_id=company_id,
        config=get_settings().jwt_config,
    )
    return {ACCESS_TOKEN_COOKIE: token}


async def _get(params: dict | None = None, cookies: dict | None = None):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", cookies=cookies
    ) as client:
        return await client.get(URL, params=params)


def _user(user_id: uuid.UUID, role_code: UserRoleCode, status: UserStatus):
    return SimpleNamespace(
        user_id=user_id,
        company_id=COMPANY_ID,
        email=f"{role_code.value.lower()}@client.com",
        display_name="홍길동",
        role_code=role_code,
        status=status,
        created_at=datetime(2026, 11, 16, 3, 0, tzinfo=UTC),
        password_hash="$2b$12$should-not-be-exposed",
    )


class FakeClientAccountService:
    """서비스는 단위 테스트에서 검증하므로, 라우터 테스트에서는 호출 인자만 기록하고 정해진 결과를 돌려준다."""

    def __init__(self):
        self.calls: list[dict] = []
        self.users = [
            _user(LEAD_ID, UserRoleCode.CLIENT_LEAD, UserStatus.ACTIVE),
            _user(MEMBER_ID, UserRoleCode.CLIENT_MEMBER, UserStatus.DISABLED),
        ]
        self.total_count = 23
        self.error: Exception | None = None

    async def list_client_users(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.users, self.total_count


@pytest.fixture
def fake_service():
    service = FakeClientAccountService()
    app.dependency_overrides[get_client_account_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_client_account_service, None)


async def test_쿠키가_없으면_401을_반환한다(fake_service):
    response = await _get({"companyId": str(COMPANY_ID)})

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHENTICATED"
    assert fake_service.calls == []


@pytest.mark.parametrize("role_code", ["OWNER", "CLIENT_LEAD", "CLIENT_MEMBER"])
async def test_ADMIN이_아니면_403을_반환한다(fake_service, role_code):
    company_id = None if role_code == "OWNER" else COMPANY_ID

    response = await _get(
        {"companyId": str(COMPANY_ID)}, cookies=_access_cookie(role_code, company_id)
    )

    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
    assert fake_service.calls == []


@pytest.mark.parametrize(
    "params",
    [
        {},  # companyId 없음
        {"companyId": "not-a-uuid"},
        {"companyId": str(COMPANY_ID), "roleCode": "ADMIN"},
        {"companyId": str(COMPANY_ID), "roleCode": "OWNER"},
        {"companyId": str(COMPANY_ID), "status": "DELETED"},
        {"companyId": str(COMPANY_ID), "page": 0},
        {"companyId": str(COMPANY_ID), "size": 101},
    ],
)
async def test_요청_형식이_틀리면_422를_반환한다(fake_service, params):
    response = await _get(params, cookies=_access_cookie("ADMIN"))

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert fake_service.calls == []


async def test_성공하면_200과_camelCase_목록을_반환한다(fake_service):
    response = await _get(
        {"companyId": str(COMPANY_ID)}, cookies=_access_cookie("ADMIN")
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"items", "page", "size", "totalCount", "totalPages"}
    assert (body["page"], body["size"]) == (1, 20)
    assert (body["totalCount"], body["totalPages"]) == (23, 2)
    # 비밀번호 관련 값과 companyId 는 행에 없다
    assert body["items"] == [
        {
            "userId": str(LEAD_ID),
            "email": "client_lead@client.com",
            "displayName": "홍길동",
            "roleCode": "CLIENT_LEAD",
            "status": "ACTIVE",
            "createdAt": "2026-11-16T03:00:00Z",
        },
        {
            "userId": str(MEMBER_ID),
            "email": "client_member@client.com",
            "displayName": "홍길동",
            "roleCode": "CLIENT_MEMBER",
            "status": "DISABLED",
            "createdAt": "2026-11-16T03:00:00Z",
        },
    ]


async def test_필터를_생략하면_None_으로_요청자와_고객사를_넘긴다(fake_service):
    await _get({"companyId": str(COMPANY_ID)}, cookies=_access_cookie("ADMIN"))

    [call] = fake_service.calls
    assert call["actor"].user_id == ADMIN_ID
    assert call["company_id"] == COMPANY_ID
    assert call["role_code"] is None
    assert call["status"] is None
    assert (call["params"].page, call["params"].size) == (1, 20)


async def test_필터와_페이지를_서비스에_넘긴다(fake_service):
    response = await _get(
        {
            "companyId": str(COMPANY_ID),
            "roleCode": "CLIENT_LEAD",
            "status": "DISABLED",
            "page": 2,
            "size": 5,
        },
        cookies=_access_cookie("ADMIN"),
    )

    assert response.status_code == 200
    [call] = fake_service.calls
    assert call["role_code"] is UserRoleCode.CLIENT_LEAD
    assert call["status"] is UserStatus.DISABLED
    assert (call["params"].page, call["params"].size) == (2, 5)
    assert (response.json()["page"], response.json()["size"]) == (2, 5)


async def test_계정이_없으면_빈_목록과_0을_반환한다(fake_service):
    fake_service.users, fake_service.total_count = [], 0

    response = await _get(
        {"companyId": str(COMPANY_ID)}, cookies=_access_cookie("ADMIN")
    )

    assert response.status_code == 200
    assert response.json() == {
        "items": [],
        "page": 1,
        "size": 20,
        "totalCount": 0,
        "totalPages": 0,
    }


async def test_서비스가_미배정을_던지면_403을_반환한다(fake_service):
    fake_service.error = ForbiddenException()

    response = await _get(
        {"companyId": str(COMPANY_ID)}, cookies=_access_cookie("ADMIN")
    )

    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
