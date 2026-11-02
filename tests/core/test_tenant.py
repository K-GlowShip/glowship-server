import uuid
from dataclasses import dataclass, field

import pytest

from app.core.exceptions import ForbiddenException
from app.core.security.auth import CurrentUser
from app.core.security.tenant import TenantContext, get_tenant_context

USER_ID = uuid.uuid7()
COMPANY_ID = uuid.uuid7()


@dataclass
class FakeSession:
    """실행한 SQL 과 파라미터만 기록한다."""

    executed: list = field(default_factory=list)

    async def execute(self, statement, params=None):
        self.executed.append((str(statement), params))


async def test_고객사_범위가_없는_토큰은_403을_던지고_DB를_건드리지_않는다():
    session = FakeSession()
    admin = CurrentUser(user_id=USER_ID, role_code="ADMIN", company_id=None)

    with pytest.raises(ForbiddenException):
        await get_tenant_context(current_user=admin, db=session)

    assert session.executed == []


async def test_고객사_사용자는_RLS_컨텍스트를_설정하고_TenantContext를_돌려준다():
    session = FakeSession()
    member = CurrentUser(
        user_id=USER_ID, role_code="CLIENT_MEMBER", company_id=COMPANY_ID
    )

    tenant = await get_tenant_context(current_user=member, db=session)

    assert tenant == TenantContext(user_id=USER_ID, company_id=COMPANY_ID)
    [(sql, params)] = session.executed
    assert "set_config('app.user_id'" in sql
    assert "set_config('app.company_id'" in sql
    assert params == {"user_id": str(USER_ID), "company_id": str(COMPANY_ID)}
