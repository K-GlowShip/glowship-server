import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from app.core.exceptions import ForbiddenException
from app.core.pagination import PageParams
from app.core.security.auth import CurrentUser
from app.modules.company.application.exception.company_exceptions import (
    CompanyInactiveException,
    CompanyNotFoundException,
)
from app.modules.user.application.client_account_service import ClientAccountService
from app.modules.user.infrastructure.user_enums import UserRoleCode, UserStatus

ADMIN = CurrentUser(user_id=uuid.uuid7(), role_code="ADMIN", company_id=None)
COMPANY_ID = uuid.uuid7()
FAKE_DB = object()
ACTIVE_COMPANY = SimpleNamespace(status="ACTIVE")
# 조회는 배정만 있으면 된다 (명세 D25). 계정 관리 권한이 없는 배정을 기본으로 둔다.
VIEW_ONLY_ASSIGNMENT = SimpleNamespace(can_manage_client_accounts=False)
USERS = [SimpleNamespace(user_id=uuid.uuid7()), SimpleNamespace(user_id=uuid.uuid7())]
PARAMS = PageParams(page=2, size=10)


# --- 가짜 리포지토리 -------------------------------------------------------------


@dataclass
class FakeCompanyRepository:
    company: object | None = None
    calls: list = field(default_factory=list)

    async def find_by_id(self, company_id):
        self.calls.append(("company", company_id))
        return self.company


@dataclass
class FakeAdminAssignmentRepository:
    assignment: object | None = None
    calls: list = field(default_factory=list)

    async def find_by_admin_and_company(self, admin_user_id, company_id):
        self.calls.append(("assignment", admin_user_id, company_id))
        return self.assignment


@dataclass
class FakeUserRepository:
    calls: list = field(default_factory=list)

    async def find_client_users_page(self, **kwargs):
        self.calls.append(("users", kwargs))
        return USERS, 12


def _service(
    *, company=ACTIVE_COMPANY, assignment=VIEW_ONLY_ASSIGNMENT
) -> ClientAccountService:
    return ClientAccountService(
        db=FAKE_DB,
        user_repo=FakeUserRepository(),
        company_repo=FakeCompanyRepository(company=company),
        admin_assignment_repo=FakeAdminAssignmentRepository(assignment=assignment),
    )


async def _list(service: ClientAccountService, **overrides):
    params = {
        "actor": ADMIN,
        "company_id": COMPANY_ID,
        "role_code": None,
        "status": None,
        "params": PARAMS,
    }
    return await service.list_client_users(**{**params, **overrides})


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


# --- 테스트 -------------------------------------------------------------------


async def test_계정_관리_권한이_없어도_배정만_있으면_목록과_전체_개수를_돌려준다():
    service = _service()

    users, total_count = await _list(service)

    assert users == USERS
    assert total_count == 12


async def test_고객사와_필터와_페이지를_그대로_조회에_넘긴다():
    service = _service()

    await _list(service, role_code=UserRoleCode.CLIENT_LEAD, status=UserStatus.DISABLED)

    assert service.user_repo.calls == [
        (
            "users",
            {
                "company_id": COMPANY_ID,
                "role_code": UserRoleCode.CLIENT_LEAD,
                "status": UserStatus.DISABLED,
                "params": PARAMS,
            },
        )
    ]


async def test_RLS_컨텍스트_배정_고객사_계정_순서로_조회한다(rls_context_calls):
    service = _service()
    service.admin_assignment_repo.calls = rls_context_calls
    service.company_repo.calls = rls_context_calls
    service.user_repo.calls = rls_context_calls

    await _list(service)

    assert [call[0] for call in rls_context_calls] == [
        "ctx",
        "assignment",
        "company",
        "users",
    ]
    assert rls_context_calls[0] == ("ctx", FAKE_DB, ADMIN.user_id, COMPANY_ID)
    assert rls_context_calls[1] == ("assignment", ADMIN.user_id, COMPANY_ID)


async def test_배정되지_않은_고객사면_고객사를_읽기_전에_403을_던진다():
    # 없는 고객사도 배정이 없으므로 같은 403 이다 (고객사가 있는지 알려 주지 않는다).
    service = _service(assignment=None, company=None)

    with pytest.raises(ForbiddenException):
        await _list(service)

    assert service.company_repo.calls == []
    assert service.user_repo.calls == []


async def test_고객사가_없으면_404를_던진다():
    service = _service(company=None)

    with pytest.raises(CompanyNotFoundException):
        await _list(service)

    assert service.user_repo.calls == []


async def test_고객사가_비활성이면_409를_던진다():
    service = _service(company=SimpleNamespace(status="INACTIVE"))

    with pytest.raises(CompanyInactiveException):
        await _list(service)

    assert service.user_repo.calls == []
