import uuid
from dataclasses import replace
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.core.exceptions import UnauthenticatedException
from app.core.security.cookies import ACCESS_TOKEN_COOKIE
from app.core.security.jwt import create_access_token, create_refresh_token
from app.main import app
from app.modules.user.application.exception.user_exceptions import (
    AccountDisabledException,
)
from app.modules.user.application.me_service import MeProfile
from app.modules.user.dependencies import get_me_service
from app.modules.user.infrastructure.user_enums import UserRoleCode

URL = "/api/v1/me"
USER_ID = uuid.uuid7()
COMPANY_ID = uuid.uuid7()
RESPONSE_KEYS = {"userId", "displayName", "roleCode", "companyId", "companyName"}


def _access_cookie(role_code: str, company_id: uuid.UUID | None = None) -> dict:
    token = create_access_token(
        user_id=USER_ID,
        role_code=role_code,
        company_id=company_id,
        config=get_settings().jwt_config,
    )
    return {ACCESS_TOKEN_COOKIE: token}


async def _get(cookies: dict | None = None):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", cookies=cookies
    ) as client:
        return await client.get(URL)


class FakeMeService:
    """서비스는 단위 테스트에서 검증하므로, 라우터 테스트에서는 호출 인자만 기록하고 정해진 결과를 돌려준다."""

    def __init__(self):
        self.calls: list = []
        self.profile = MeProfile(
            user_id=USER_ID,
            display_name="홍길동",
            role_code=UserRoleCode.CLIENT_LEAD,
            company_id=COMPANY_ID,
            company_name="A Beauty",
        )
        self.error: Exception | None = None

    async def get_me(self, current_user):
        self.calls.append(current_user)
        if self.error is not None:
            raise self.error
        return self.profile


@pytest.fixture
def fake_service():
    service = FakeMeService()
    app.dependency_overrides[get_me_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_me_service, None)


async def test_쿠키가_없으면_401을_반환한다(fake_service):
    response = await _get()

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHENTICATED"
    assert fake_service.calls == []


async def test_만료된_토큰은_401을_반환한다(fake_service):
    config = get_settings().jwt_config
    expired = replace(config, access_token_expires=timedelta(seconds=-1))
    token = create_access_token(
        user_id=USER_ID, role_code="CLIENT_LEAD", company_id=COMPANY_ID, config=expired
    )

    response = await _get({ACCESS_TOKEN_COOKIE: token})

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHENTICATED"
    assert fake_service.calls == []


async def test_refresh_토큰을_access_쿠키로_보내면_401을_반환한다(fake_service):
    token = create_refresh_token(user_id=USER_ID, config=get_settings().jwt_config)

    response = await _get({ACCESS_TOKEN_COOKIE: token})

    assert response.status_code == 401
    assert fake_service.calls == []


async def test_성공하면_200과_camelCase_응답을_반환한다(fake_service):
    response = await _get(_access_cookie("CLIENT_LEAD", company_id=COMPANY_ID))

    assert response.status_code == 200
    body = response.json()
    assert set(body) == RESPONSE_KEYS
    assert body == {
        "userId": str(USER_ID),
        "displayName": "홍길동",
        "roleCode": "CLIENT_LEAD",
        "companyId": str(COMPANY_ID),
        "companyName": "A Beauty",
    }

    [current_user] = fake_service.calls
    assert current_user.user_id == USER_ID
    assert current_user.role_code == "CLIENT_LEAD"
    assert current_user.company_id == COMPANY_ID


async def test_소속_고객사가_없으면_고객사_필드를_null_로_보낸다(fake_service):
    fake_service.profile = MeProfile(
        user_id=USER_ID,
        display_name="admin@glowship.com",
        role_code=UserRoleCode.ADMIN,
        company_id=None,
        company_name=None,
    )

    response = await _get(_access_cookie("ADMIN"))

    assert response.status_code == 200
    body = response.json()
    assert set(body) == RESPONSE_KEYS  # 값이 없어도 필드를 빼지 않는다
    assert body["roleCode"] == "ADMIN"
    assert body["companyId"] is None
    assert body["companyName"] is None


async def test_서비스가_사용자_없음을_던지면_401을_반환한다(fake_service):
    fake_service.error = UnauthenticatedException()

    response = await _get(_access_cookie("CLIENT_LEAD", company_id=COMPANY_ID))

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHENTICATED"


async def test_서비스가_비활성_계정을_던지면_403을_반환한다(fake_service):
    fake_service.error = AccountDisabledException()

    response = await _get(_access_cookie("CLIENT_LEAD", company_id=COMPANY_ID))

    assert response.status_code == 403
    assert response.json()["code"] == "ACCOUNT_DISABLED"
