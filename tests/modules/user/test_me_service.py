import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from app.core.exceptions import UnauthenticatedException
from app.core.security.auth import CurrentUser
from app.modules.company.infrastructure.repository.admin_assignment_repository import (
    AssignedCompany,
)
from app.modules.user.application.exception.user_exceptions import (
    AccountDisabledException,
)
from app.modules.user.application.me_service import MeProfile, MeService
from app.modules.user.infrastructure.user_enums import UserRoleCode, UserStatus

USER_ID = uuid.uuid7()
COMPANY_ID = uuid.uuid7()
FAKE_DB = object()
ACTIVE_COMPANY = SimpleNamespace(company_name="A Beauty")

CLIENT_LEAD = CurrentUser(
    user_id=USER_ID, role_code="CLIENT_LEAD", company_id=COMPANY_ID
)
ADMIN = CurrentUser(user_id=USER_ID, role_code="ADMIN", company_id=None)
OWNER = CurrentUser(user_id=USER_ID, role_code="OWNER", company_id=None)

ASSIGNED_COMPANIES = [
    AssignedCompany(
        company_id=COMPANY_ID, company_name="A Beauty", can_manage_client_accounts=True
    ),
    AssignedCompany(
        company_id=uuid.uuid7(),
        company_name="B Cosmetics",
        can_manage_client_accounts=False,
    ),
]


def _user(
    *,
    role_code: UserRoleCode = UserRoleCode.CLIENT_LEAD,
    company_id: uuid.UUID | None = COMPANY_ID,
    status: UserStatus = UserStatus.ACTIVE,
    display_name: str = "홍길동",
):
    return SimpleNamespace(
        user_id=USER_ID,
        company_id=company_id,
        role_code=role_code,
        status=status,
        email="lead@client.com",
        display_name=display_name,
        password_hash="$2b$12$should-not-be-exposed",
    )


# --- 가짜 리포지토리 -------------------------------------------------------------


@dataclass
class FakeUserRepository:
    user: object | None = None
    calls: list = field(default_factory=list)

    async def find_by_id(self, user_id):
        self.calls.append(("user", user_id))
        return self.user


@dataclass
class FakeCompanyRepository:
    company: object | None = None
    calls: list = field(default_factory=list)

    async def find_by_id(self, company_id):
        self.calls.append(("company", company_id))
        return self.company


@dataclass
class FakeAdminAssignmentRepository:
    assigned_companies: list = field(default_factory=list)
    calls: list = field(default_factory=list)

    async def find_assigned_companies(self):
        self.calls.append(("assigned",))
        return list(self.assigned_companies)


def _service(
    *, user=None, company=ACTIVE_COMPANY, assigned_companies=()
) -> tuple[MeService, FakeUserRepository, FakeCompanyRepository]:
    """담당 고객사 가짜 리포지토리는 service.admin_assignment_repo 로 꺼내 본다."""
    user_repo = FakeUserRepository(user=user)
    company_repo = FakeCompanyRepository(company=company)
    service = MeService(
        db=FAKE_DB,
        user_repo=user_repo,
        company_repo=company_repo,
        admin_assignment_repo=FakeAdminAssignmentRepository(
            assigned_companies=list(assigned_companies)
        ),
    )
    return service, user_repo, company_repo


@pytest.fixture(autouse=True)
def rls_context_calls(monkeypatch):
    """DB 없이 실행하므로 set_rls_context 를 기록용 가짜로 바꾼다. 호출 순서 검증에 쓴다."""
    calls: list = []

    async def _fake_set_rls_context(db, *, user_id, company_id):
        calls.append(("ctx", db, user_id, company_id))

    monkeypatch.setattr(
        "app.modules.user.application.me_service.set_rls_context",
        _fake_set_rls_context,
    )
    return calls


# --- 테스트 -------------------------------------------------------------------


async def test_고객사_사용자는_DB_행과_고객사명을_돌려준다():
    service, _, _ = _service(user=_user())

    profile = await service.get_me(CLIENT_LEAD)

    assert profile == MeProfile(
        user_id=USER_ID,
        display_name="홍길동",
        role_code=UserRoleCode.CLIENT_LEAD,
        company_id=COMPANY_ID,
        company_name="A Beauty",
        assigned_companies=(),
    )


async def test_값의_출처는_토큰이_아니라_DB_행이다():
    # 토큰은 CLIENT_LEAD 지만 DB 가 CLIENT_MEMBER 면 DB 값을 돌려준다 (토큰 발급 뒤 역할이 바뀐 경우).
    service, _, _ = _service(user=_user(role_code=UserRoleCode.CLIENT_MEMBER))

    profile = await service.get_me(CLIENT_LEAD)

    assert profile.role_code == UserRoleCode.CLIENT_MEMBER


@pytest.mark.parametrize(
    ("current_user", "role_code"),
    [(ADMIN, UserRoleCode.ADMIN), (OWNER, UserRoleCode.OWNER)],
)
async def test_소속_고객사가_없는_역할은_고객사_필드가_None_이고_companies_를_읽지_않는다(
    current_user, role_code
):
    service, _, company_repo = _service(
        user=_user(role_code=role_code, company_id=None)
    )

    profile = await service.get_me(current_user)

    assert profile.role_code == role_code
    assert profile.company_id is None
    assert profile.company_name is None
    assert company_repo.calls == []


@pytest.mark.parametrize(
    ("current_user", "role_code"),
    [(ADMIN, UserRoleCode.ADMIN), (OWNER, UserRoleCode.OWNER)],
)
async def test_ADMIN_과_OWNER_는_담당_고객사_목록을_순서_그대로_돌려준다(
    current_user, role_code
):
    service, _, _ = _service(
        user=_user(role_code=role_code, company_id=None),
        assigned_companies=ASSIGNED_COMPANIES,
    )

    profile = await service.get_me(current_user)

    assert profile.assigned_companies == tuple(ASSIGNED_COMPANIES)


async def test_배정이_없는_ADMIN_은_담당_고객사가_빈_목록이다():
    service, _, _ = _service(user=_user(role_code=UserRoleCode.ADMIN, company_id=None))

    profile = await service.get_me(ADMIN)

    assert profile.assigned_companies == ()
    assert service.admin_assignment_repo.calls == [("assigned",)]


async def test_고객사_사용자는_담당_고객사를_조회하지_않고_빈_목록이다():
    service, _, _ = _service(user=_user(), assigned_companies=ASSIGNED_COMPANIES)

    profile = await service.get_me(CLIENT_LEAD)

    assert profile.assigned_companies == ()
    assert service.admin_assignment_repo.calls == []


async def test_담당_고객사_조회_여부는_토큰이_아니라_DB_역할로_정한다():
    # 토큰은 ADMIN 이지만 DB 가 CLIENT_LEAD 면 조회하지 않는다 (토큰 발급 뒤 역할이 바뀐 경우).
    service, _, _ = _service(user=_user(), assigned_companies=ASSIGNED_COMPANIES)

    profile = await service.get_me(ADMIN)

    assert profile.assigned_companies == ()
    assert service.admin_assignment_repo.calls == []


async def test_담당_고객사는_RLS_컨텍스트와_계정_검사_뒤에_조회한다(rls_context_calls):
    service, user_repo, _ = _service(
        user=_user(role_code=UserRoleCode.ADMIN, company_id=None)
    )
    user_repo.calls = rls_context_calls
    service.admin_assignment_repo.calls = rls_context_calls

    await service.get_me(ADMIN)

    assert rls_context_calls == [
        ("ctx", FAKE_DB, USER_ID, None),
        ("user", USER_ID),
        ("assigned",),
    ]


async def test_RLS_컨텍스트는_첫_DB_조회_전에_토큰의_사용자와_고객사로_설정한다(
    rls_context_calls,
):
    service, user_repo, company_repo = _service(user=_user())
    # 조회 기록을 컨텍스트 기록과 같은 리스트에 남겨 호출 순서를 본다.
    user_repo.calls = rls_context_calls
    company_repo.calls = rls_context_calls

    await service.get_me(CLIENT_LEAD)

    assert rls_context_calls == [
        ("ctx", FAKE_DB, USER_ID, COMPANY_ID),
        ("user", USER_ID),
        ("company", COMPANY_ID),
    ]


async def test_토큰의_사용자가_DB_에_없으면_401을_던진다():
    service, _, company_repo = _service(user=None)

    with pytest.raises(UnauthenticatedException):
        await service.get_me(CLIENT_LEAD)

    assert company_repo.calls == []
    assert service.admin_assignment_repo.calls == []


async def test_비활성_계정이면_403을_던진다():
    service, _, company_repo = _service(user=_user(status=UserStatus.DISABLED))

    with pytest.raises(AccountDisabledException):
        await service.get_me(CLIENT_LEAD)

    assert company_repo.calls == []


async def test_비활성_ADMIN_은_담당_고객사를_조회하기_전에_403을_던진다():
    service, _, _ = _service(
        user=_user(
            role_code=UserRoleCode.ADMIN, company_id=None, status=UserStatus.DISABLED
        ),
        assigned_companies=ASSIGNED_COMPANIES,
    )

    with pytest.raises(AccountDisabledException):
        await service.get_me(ADMIN)

    assert service.admin_assignment_repo.calls == []


async def test_고객사_행이_없으면_고객사명만_None_이다():
    service, _, _ = _service(user=_user(), company=None)

    profile = await service.get_me(CLIENT_LEAD)

    assert profile.company_id == COMPANY_ID
    assert profile.company_name is None
