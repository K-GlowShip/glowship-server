import base64
import uuid
from dataclasses import dataclass, field

import pytest
from sqlalchemy.exc import DBAPIError

from app.core.exceptions import ForbiddenException
from app.core.security.auth import CurrentUser
from app.modules.user.application.client_account_service import ClientAccountService
from app.modules.user.application.exception.user_exceptions import (
    InvalidPasswordFormatException,
    UserNotFoundException,
)

ADMIN = CurrentUser(user_id=uuid.uuid7(), role_code="ADMIN", company_id=None)
TARGET_USER_ID = uuid.uuid7()
ENCODED_PASSWORD = base64.b64encode(b"newpass123").decode()
FAKE_DB = object()


def _db_error(pgcode: str) -> DBAPIError:
    orig = Exception(pgcode)
    orig.pgcode = pgcode
    return DBAPIError("SELECT", {}, orig)


# --- 가짜 리포지토리 -------------------------------------------------------------


@dataclass
class FakeUserRepository:
    error: Exception | None = None
    calls: list = field(default_factory=list)

    async def reset_client_user_password(self, **kwargs):
        self.calls.append(("reset", kwargs))
        if self.error is not None:
            raise self.error


def _service(*, error=None) -> ClientAccountService:
    # 배정·고객사 검사는 DB 함수가 하므로 이 경로는 두 리포지토리를 쓰지 않는다 (쓰면 None 이라 바로 드러난다).
    return ClientAccountService(
        db=FAKE_DB,
        user_repo=FakeUserRepository(error=error),
        company_repo=None,
        admin_assignment_repo=None,
    )


async def _reset(service: ClientAccountService, **overrides):
    params = {
        "actor": ADMIN,
        "user_id": TARGET_USER_ID,
        "encoded_password": ENCODED_PASSWORD,
    }
    return await service.reset_client_user_password(**{**params, **overrides})


@pytest.fixture(autouse=True)
def rls_context_calls(monkeypatch):
    """DB 없이 실행하므로 set_rls_context 를 기록용 가짜로 바꾼다. 호출 순서 검증에 쓴다."""
    calls: list = []

    async def _fake_set_rls_context(db, *, user_id, company_id):
        calls.append(("ctx", db, user_id, company_id))

    monkeypatch.setattr(
        "app.modules.user.application.client_account_service.set_rls_context",
        _fake_set_rls_context,
    )
    return calls


@pytest.fixture(autouse=True)
def fast_hash(monkeypatch):
    """bcrypt 는 느리므로 테스트에서는 해시 함수를 대체한다. 저장되는 값이 해시인지는 호출 인자로 확인한다."""

    async def _fake_hash(raw_password: str) -> str:
        return f"hashed:{raw_password}"

    monkeypatch.setattr(
        "app.modules.user.application.client_account_service.hash_password",
        _fake_hash,
    )


# --- 테스트 -------------------------------------------------------------------


async def test_성공하면_대상_계정에_해시된_새_비밀번호를_저장한다():
    service = _service()

    assert await _reset(service) is None

    assert service.user_repo.calls == [
        ("reset", {"user_id": TARGET_USER_ID, "password_hash": "hashed:newpass123"})
    ]


async def test_RLS_컨텍스트는_DB_함수_호출_전에_요청자만_넣고_고객사는_비운다(
    rls_context_calls,
):
    service = _service()
    service.user_repo.calls = rls_context_calls

    await _reset(service)

    assert rls_context_calls[0] == ("ctx", FAKE_DB, ADMIN.user_id, None)
    assert [call[0] for call in rls_context_calls] == ["ctx", "reset"]


@pytest.mark.parametrize(
    "encoded_password",
    [
        "not-base64!!",
        base64.b64encode(b"short").decode(),  # 5자
        base64.b64encode(b"a" * 17).decode(),  # 17자
        base64.b64encode(b"pass word1").decode(),  # 공백
        base64.b64encode("비밀번호123".encode()).decode(),  # 영문·숫자 아님
    ],
)
async def test_비밀번호_형식이_틀리면_DB_에_가지_않고_400을_던진다(
    encoded_password, rls_context_calls
):
    service = _service()

    with pytest.raises(InvalidPasswordFormatException):
        await _reset(service, encoded_password=encoded_password)

    assert rls_context_calls == []
    assert service.user_repo.calls == []


async def test_DB_함수가_대상을_찾지_못하면_404를_던진다():
    # 없는 계정·배정되지 않은 고객사의 계정·Client 역할이 아닌 계정은 함수가 같은 오류(P0002)로 알린다.
    service = _service(error=_db_error("P0002"))

    with pytest.raises(UserNotFoundException):
        await _reset(service)


async def test_DB_함수의_권한_오류는_403으로_변환한다():
    service = _service(error=_db_error("42501"))

    with pytest.raises(ForbiddenException):
        await _reset(service)


async def test_그_밖의_DB_오류는_그대로_전파한다():
    service = _service(error=_db_error("40P01"))

    with pytest.raises(DBAPIError):
        await _reset(service)
