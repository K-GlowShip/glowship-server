import base64
import uuid

import pytest

from app.core.config import get_settings
from app.core.security.hashing import hash_password
from app.core.security.jwt import TokenType, decode_token
from app.modules.user.application.auth_service import AuthService
from app.modules.user.application.exception.user_exceptions import (
    AccountDisabledException,
    InvalidCredentialsException,
)
from app.modules.user.infrastructure.repository.user_repository import LoginUser
from app.modules.user.infrastructure.user_enums import UserRoleCode, UserStatus

"""
    로그인 (AuthService.login) — 로그인 조회는 DB 함수 경로(find_login_user)를 쓴다 (이슈 #41)
      - 운영 앱 DB 역할에서는 컨텍스트 없이 select(User) 가 0행이라, 서비스가 find_by_email 을 부르면 안 된다.
"""

PASSWORD = "Glow-Demo1!"
USER_ID = uuid.uuid7()
COMPANY_ID = uuid.uuid7()


def _encoded(raw: str) -> str:
    return base64.b64encode(raw.encode()).decode()


class FakeUserRepository:
    def __init__(self, user: LoginUser | None):
        self.user = user
        self.calls: list[str] = []

    async def find_login_user(self, email: str) -> LoginUser | None:
        self.calls.append(email)
        return self.user

    async def find_by_email(self, email: str):
        raise AssertionError("로그인은 find_by_email(select User)을 쓰지 않는다")


async def _user(status: UserStatus = UserStatus.ACTIVE) -> LoginUser:
    return LoginUser(
        user_id=USER_ID,
        company_id=COMPANY_ID,
        role_code=UserRoleCode.CLIENT_MEMBER,
        password_hash=await hash_password(PASSWORD),
        status=status,
    )


def _service(repo: FakeUserRepository) -> AuthService:
    return AuthService(user_repo=repo, jwt_config=get_settings().jwt_config)


async def test_login_uses_login_lookup_and_issues_tokens():
    repo = FakeUserRepository(await _user())

    tokens = await _service(repo).login("member@a.example", _encoded(PASSWORD))

    payload = decode_token(
        tokens.access_token, get_settings().jwt_config, TokenType.ACCESS
    )
    assert repo.calls == ["member@a.example"]
    assert payload["sub"] == str(USER_ID)
    assert payload["role"] == "CLIENT_MEMBER"
    assert payload["company_id"] == str(COMPANY_ID)


@pytest.mark.parametrize("found", [True, False])
async def test_unknown_email_or_wrong_password_is_401(found):
    repo = FakeUserRepository(await _user() if found else None)

    with pytest.raises(InvalidCredentialsException):
        await _service(repo).login("member@a.example", _encoded("Wrong-pass1!"))


async def test_disabled_account_is_403():
    repo = FakeUserRepository(await _user(UserStatus.DISABLED))

    with pytest.raises(AccountDisabledException):
        await _service(repo).login("member@a.example", _encoded(PASSWORD))
