import uuid
from dataclasses import dataclass

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.exceptions import ForbiddenException
from app.core.rls_context import set_rls_context
from app.core.security.auth import CurrentUser, get_current_user

"""
    고객사 범위(테넌트) 의존성

    Client 사용자(CLIENT_MEMBER·CLIENT_LEAD)가 자기 고객사 데이터를 조회하는 API 가 공통으로 쓴다.
      - access 토큰의 company_id 를 고객사 범위로 삼는다. OWNER·ADMIN 은 소속 고객사가 없어 403 이다.
        (ADMIN 의 고객사 선택 방식(명세 D7)은 아직 없다)
      - 첫 DB 조회 전에 같은 트랜잭션에 RLS 컨텍스트를 설정한다. 운영(앱 DB 역할)에서는 이 설정이 없으면
        고객사 범위 테이블의 행이 보이지 않는다. get_db 는 요청당 한 세션이므로 뒤따르는 리포지토리 조회에 적용된다.

        @router.get("")
        async def list_orders(tenant: TenantContext = Depends(get_tenant_context)): ...
"""


@dataclass(frozen=True)
class TenantContext:
    """요청 주체와 이번 요청이 다루는 고객사"""

    user_id: uuid.UUID
    company_id: uuid.UUID


async def get_tenant_context(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TenantContext:
    """토큰의 company_id 로 RLS 컨텍스트를 설정하고 TenantContext 를 돌려준다. 고객사 범위가 없으면 403."""
    if current_user.company_id is None:
        raise ForbiddenException()

    await set_rls_context(
        db, user_id=current_user.user_id, company_id=current_user.company_id
    )
    return TenantContext(
        user_id=current_user.user_id, company_id=current_user.company_id
    )
