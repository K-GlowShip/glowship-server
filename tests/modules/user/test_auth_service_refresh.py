import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt as pyjwt
import pytest

from app.core.config import get_settings
from app.core.exceptions import UnauthenticatedException
from app.core.security.jwt import (
    TokenType,
    create_access_token,
    create_refresh_token,
    decode_token,
)
from app.modules.user.application.auth_service import AuthService
from app.modules.user.application.exception.user_exceptions import (
    AccountDisabledException,
)
from app.modules.user.infrastructure.user_enums import UserRoleCode, UserStatus

"""
    접근 토큰 재발급 (AuthService.refresh_access_token, API 27, GLOW-152)
      - refresh 토큰에는 역할·고객사가 없으므로 DB 의 현재 값으로 access 토큰을 만든다.
      - 운영 앱 DB 역할에서 자기 행을 읽으려면 조회 전에 RLS 컨텍스트(app.user_id)가 있어야 한다.
      - refresh 토큰은 회전하지 않는다 (access 토큰 문자열 하나만 돌려준다).
"""

USER_ID = uuid.uuid7()
COMPANY_ID = uuid.uuid7()
FAKE_DB = object()
CONFIG = get_settings().jwt_config


def _user(
    *,
    role_code: UserRoleCode = UserRoleCode.CLIENT_LEAD,
    company_id: uuid.UUID | None = COMPANY_ID,
    status: UserStatus = UserStatus.ACTIVE,
):
    return SimpleNamespace(
        user_id=USER_ID,
        company_id=company_id,
        role_code=role_code,
        status=status,
        password_hash="$2b$12$should-not-be-exposed",
    )


@dataclass
class FakeUserRepository:
    user: object | None = None
    calls: list = field(default_factory=list)

    async def find_by_id(self, user_id):
        self.calls.append(("user", user_id))
        return self.user

    async def find_login_user(self, email):
        raise AssertionError("재발급은 이메일 조회를 하지 않는다")


def _service(user=None) -> tuple[AuthService, FakeUserRepository]:
    repo = FakeUserRepository(user=user)
    return AuthService(db=FAKE_DB, user_repo=repo, jwt_config=CONFIG), repo


def _refresh_token(config=CONFIG, user_id: uuid.UUID = USER_ID) -> str:
    return create_refresh_token(user_id=user_id, config=config)


@pytest.fixture(autouse=True)
def rls_context_calls(monkeypatch):
    """DB 없이 실행하므로 set_rls_context 를 기록용 가짜로 바꾼다. 호출 순서 검증에 쓴다."""
    calls: list = []

    async def _fake_set_rls_context(db, *, user_id, company_id):
        calls.append(("ctx", db, user_id, company_id))

    monkeypatch.setattr(
        "app.modules.user.application.auth_service.set_rls_context",
        _fake_set_rls_context,
    )
    return calls


# --- 성공 ---------------------------------------------------------------------


async def test_DB_의_현재_역할과_고객사로_access_토큰을_발급한다():
    service, repo = _service(user=_user())

    access_token = await service.refresh_access_token(_refresh_token())

    payload = decode_token(access_token, CONFIG, TokenType.ACCESS)
    assert payload["sub"] == str(USER_ID)
    assert payload["role"] == "CLIENT_LEAD"
    assert payload["company_id"] == str(COMPANY_ID)
    assert repo.calls == [("user", USER_ID)]


@pytest.mark.parametrize("role_code", [UserRoleCode.ADMIN, UserRoleCode.OWNER])
async def test_소속_고객사가_없는_역할은_company_id_를_null_로_명시한다(role_code):
    service, _ = _service(user=_user(role_code=role_code, company_id=None))

    access_token = await service.refresh_access_token(_refresh_token())

    payload = decode_token(access_token, CONFIG, TokenType.ACCESS)
    assert payload["role"] == role_code.value
    assert "company_id" in payload
    assert payload["company_id"] is None


async def test_RLS_컨텍스트는_첫_DB_조회_전에_토큰의_사용자로_설정한다(
    rls_context_calls,
):
    service, repo = _service(user=_user())
    order: list = []
    original_find_by_id = repo.find_by_id

    async def _find_by_id(user_id):
        order.append(("find", len(rls_context_calls)))
        return await original_find_by_id(user_id)

    repo.find_by_id = _find_by_id

    await service.refresh_access_token(_refresh_token())

    # 조회 시점에 컨텍스트 호출이 이미 1건 있어야 한다. 고객사 컨텍스트는 비운다(자기 행은 user_id 조건으로 보임).
    assert order == [("find", 1)]
    assert rls_context_calls == [("ctx", FAKE_DB, USER_ID, None)]


# --- 토큰이 신뢰할 수 없을 때 (401) ---------------------------------------------------


async def test_만료된_refresh_토큰은_401이고_DB_를_읽지_않는다(rls_context_calls):
    service, repo = _service(user=_user())
    expired = replace(CONFIG, refresh_token_expires=timedelta(seconds=-1))

    with pytest.raises(UnauthenticatedException):
        await service.refresh_access_token(_refresh_token(config=expired))

    assert repo.calls == []
    assert rls_context_calls == []


async def test_access_토큰을_refresh_토큰_자리에_보내면_401이다():
    service, repo = _service(user=_user())
    access_token = create_access_token(
        user_id=USER_ID, role_code="CLIENT_LEAD", company_id=COMPANY_ID, config=CONFIG
    )

    with pytest.raises(UnauthenticatedException):
        await service.refresh_access_token(access_token)

    assert repo.calls == []


@pytest.mark.parametrize("token", ["not-a-jwt", ""])
async def test_위조되거나_빈_토큰은_401이다(token):
    service, repo = _service(user=_user())

    with pytest.raises(UnauthenticatedException):
        await service.refresh_access_token(token)

    assert repo.calls == []


async def test_다른_키로_서명한_토큰은_401이다():
    service, repo = _service(user=_user())
    other = replace(CONFIG, secret_key="x" * 48)

    with pytest.raises(UnauthenticatedException):
        await service.refresh_access_token(_refresh_token(config=other))

    assert repo.calls == []


async def test_sub_가_UUID_가_아니면_401이다():
    service, repo = _service(user=_user())
    now = datetime.now(UTC)
    token = pyjwt.encode(
        {
            "sub": "not-a-uuid",
            "type": TokenType.REFRESH.value,
            "iat": now,
            "exp": now + timedelta(days=1),
            "jti": uuid.uuid7().hex,
        },
        CONFIG.secret_key,
        algorithm=CONFIG.algorithm,
    )

    with pytest.raises(UnauthenticatedException):
        await service.refresh_access_token(token)

    assert repo.calls == []


# --- 토큰은 유효하나 계정 상태가 맞지 않을 때 ------------------------------------------


async def test_토큰의_사용자가_DB_에_없으면_401이다():
    service, repo = _service(user=None)

    with pytest.raises(UnauthenticatedException):
        await service.refresh_access_token(_refresh_token())

    assert repo.calls == [("user", USER_ID)]


async def test_비활성_계정은_403이다():
    service, _ = _service(user=_user(status=UserStatus.DISABLED))

    with pytest.raises(AccountDisabledException):
        await service.refresh_access_token(_refresh_token())
