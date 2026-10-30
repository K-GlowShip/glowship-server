import base64
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.exceptions import ForbiddenException
from app.core.security.auth import CurrentUser
from app.modules.company.application.exception.company_exceptions import (
    CompanyInactiveException,
    CompanyNotFoundException,
)
from app.modules.user.application.client_account_service import ClientAccountService
from app.modules.user.application.exception.user_exceptions import (
    EmailDuplicatedException,
    InvalidPasswordFormatException,
)
from app.modules.user.infrastructure.user_enums import UserRoleCode

ADMIN = CurrentUser(user_id=uuid.uuid7(), role_code="ADMIN", company_id=None)
COMPANY_ID = uuid.uuid7()
EMAIL = "lead@client.com"
ENCODED_PASSWORD = base64.b64encode(b"password123").decode()
ACTIVE_COMPANY = SimpleNamespace(status="ACTIVE")
MANAGING_ASSIGNMENT = SimpleNamespace(can_manage_client_accounts=True)


# --- 가짜 리포지토리 -------------------------------------------------------------


@dataclass
class FakeCompanyRepository:
    company: object | None = None

    async def find_by_id(self, company_id):
        return self.company


@dataclass
class FakeAdminAssignmentRepository:
    assignment: object | None = None

    async def find_by_admin_and_company(self, admin_user_id, company_id):
        return self.assignment


@dataclass
class FakeUserRepository:
    existing: object | None = None
    error: Exception | None = None
    created: dict = field(default_factory=dict)

    async def find_by_email(self, email):
        return self.existing

    async def create_client_user(self, **kwargs):
        if self.error is not None:
            raise self.error
        self.created = kwargs
        return SimpleNamespace(**kwargs)


def _service(
    *,
    company=ACTIVE_COMPANY,
    assignment=MANAGING_ASSIGNMENT,
    existing=None,
    error=None,
) -> tuple[ClientAccountService, FakeUserRepository]:
    user_repo = FakeUserRepository(existing=existing, error=error)
    service = ClientAccountService(
        user_repo=user_repo,
        company_repo=FakeCompanyRepository(company=company),
        admin_assignment_repo=FakeAdminAssignmentRepository(assignment=assignment),
    )
    return service, user_repo


async def _create(service: ClientAccountService, **overrides):
    params = {
        "actor": ADMIN,
        "company_id": COMPANY_ID,
        "email": EMAIL,
        "encoded_password": ENCODED_PASSWORD,
        "role_code": UserRoleCode.CLIENT_LEAD,
        "display_name": None,
    }
    return await service.create_client_user(**{**params, **overrides})


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


async def test_성공하면_요청자와_고객사_컨텍스트로_해시된_비밀번호를_저장한다():
    service, user_repo = _service()

    user = await _create(service)

    created = user_repo.created
    assert created["actor_user_id"] == ADMIN.user_id
    assert created["company_id"] == COMPANY_ID
    assert created["email"] == EMAIL
    assert created["password_hash"] == "hashed:password123"
    assert created["role_code"] == UserRoleCode.CLIENT_LEAD
    assert isinstance(created["user_id"], uuid.UUID)
    assert user.user_id == created["user_id"]


async def test_표시_이름이_없으면_이메일을_쓴다():
    service, user_repo = _service()

    await _create(service, display_name=None)
    assert user_repo.created["display_name"] == EMAIL

    await _create(service, display_name="홍길동")
    assert user_repo.created["display_name"] == "홍길동"


async def test_비밀번호_정책_위반이면_DB를_조회하지_않고_400을_던진다():
    service, user_repo = _service(company=None)  # 고객사 조회가 일어나면 404 가 났을 것

    with pytest.raises(InvalidPasswordFormatException):
        await _create(service, encoded_password=base64.b64encode(b"short").decode())

    assert user_repo.created == {}


async def test_고객사가_없으면_404를_던진다():
    service, _ = _service(company=None)

    with pytest.raises(CompanyNotFoundException):
        await _create(service)


async def test_고객사가_비활성이면_409를_던진다():
    service, _ = _service(company=SimpleNamespace(status="INACTIVE"))

    with pytest.raises(CompanyInactiveException):
        await _create(service)


async def test_배정되지_않은_고객사면_403을_던진다():
    service, _ = _service(assignment=None)

    with pytest.raises(ForbiddenException):
        await _create(service)


async def test_계정_관리_권한이_없으면_403을_던진다():
    service, _ = _service(assignment=SimpleNamespace(can_manage_client_accounts=False))

    with pytest.raises(ForbiddenException):
        await _create(service)


async def test_이메일이_이미_있으면_409를_던진다():
    service, user_repo = _service(existing=SimpleNamespace(email=EMAIL))

    with pytest.raises(EmailDuplicatedException):
        await _create(service)

    assert user_repo.created == {}


async def test_DB_유니크_위반은_409로_변환한다():
    error = IntegrityError("INSERT", {}, Exception("unique_violation"))
    service, _ = _service(error=error)

    with pytest.raises(EmailDuplicatedException):
        await _create(service)


async def test_DB_함수의_권한_오류는_403으로_변환한다():
    orig = Exception("insufficient_privilege")
    orig.pgcode = "42501"
    service, _ = _service(error=DBAPIError("SELECT", {}, orig))

    with pytest.raises(ForbiddenException):
        await _create(service)


async def test_그_밖의_DB_오류는_그대로_전파한다():
    orig = Exception("deadlock")
    orig.pgcode = "40P01"
    error = DBAPIError("SELECT", {}, orig)
    service, _ = _service(error=error)

    with pytest.raises(DBAPIError):
        await _create(service)
