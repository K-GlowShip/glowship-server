import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import UnauthenticatedException
from app.core.rls_context import set_rls_context
from app.core.security.auth import CurrentUser
from app.modules.company.infrastructure.repository.company_repository import (
    CompanyRepository,
)
from app.modules.user.application.exception.user_exceptions import (
    AccountDisabledException,
)
from app.modules.user.infrastructure.repository.user_repository import UserRepository
from app.modules.user.infrastructure.user_enums import UserRoleCode, UserStatus

"""
    내 계정 조회 서비스 (API 명세 v6.2 4.1 GET /me, 공통 헤더)
      - 로그인 응답에는 사용자 정보가 없으므로(C31) 로그인 직후 화면이 이 API 로 계정·역할·고객사명을 받는다.
      - 값의 출처는 토큰이 아니라 DB 행이다. 토큰 발급 뒤 표시 이름·역할이 바뀌어도 현재 값을 돌려준다.
      - 명세의 email 과 ADMIN 의 assignedCompanies[] 는 프론트 합의로 응답에 넣지 않는다 (docs/auth/me.md).
"""


@dataclass(frozen=True)
class MeProfile:
    """내 계정 정보. 소속 고객사가 없는 OWNER·ADMIN 은 company_id·company_name 이 None 이다."""

    user_id: uuid.UUID
    display_name: str
    role_code: UserRoleCode
    company_id: uuid.UUID | None
    company_name: str | None


class MeService:
    def __init__(
        self,
        db: AsyncSession,
        user_repo: UserRepository,
        company_repo: CompanyRepository,
    ):
        # 리포지토리들과 같은 요청 단위 세션. RLS 컨텍스트 설정에만 사용한다.
        self.db = db
        self.user_repo = user_repo
        self.company_repo = company_repo

    async def get_me(self, current_user: CurrentUser) -> MeProfile:
        """
        access 토큰의 사용자를 DB 에서 다시 읽어 계정·역할·고객사명을 돌려준다.

        Raises:
            UnauthenticatedException: 토큰의 사용자가 DB 에 없음 (401, 세션이 유효하지 않은 것으로 본다)
            AccountDisabledException: 계정이 DISABLED (403). 토큰 만료 전에 비활성화된 계정을 여기서 막고,
                프론트가 재발급을 시도하는 401 과 구분한다
        """
        # 첫 DB 조회 전에 RLS 컨텍스트를 설정한다. 운영(앱 DB 역할)에서는 users 의 self 정책이 app.user_id 로,
        # companies 의 고객사 정책이 app.company_id 로 행을 보여 준다. (로컬 superuser 는 정책을 우회)
        await set_rls_context(
            self.db, user_id=current_user.user_id, company_id=current_user.company_id
        )

        user = await self.user_repo.find_by_id(current_user.user_id)
        if user is None:
            raise UnauthenticatedException()
        if user.status != UserStatus.ACTIVE:
            raise AccountDisabledException()

        # Client 역할만 소속 고객사가 있다 (users_ck2). OWNER·ADMIN 은 companies 를 읽지 않는다.
        company_name = None
        if user.company_id is not None:
            company = await self.company_repo.find_by_id(user.company_id)
            company_name = company.company_name if company is not None else None

        return MeProfile(
            user_id=user.user_id,
            display_name=user.display_name,
            role_code=UserRoleCode(user.role_code),
            company_id=user.company_id,
            company_name=company_name,
        )
