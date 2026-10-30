import uuid

from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.exceptions import ForbiddenException
from app.core.security.auth import CurrentUser
from app.core.security.hashing import hash_password
from app.modules.company.application.exception.company_exceptions import (
    CompanyInactiveException,
    CompanyNotFoundException,
)
from app.modules.company.infrastructure.company_enums import CompanyStatus
from app.modules.company.infrastructure.repository.admin_assignment_repository import (
    AdminAssignmentRepository,
)
from app.modules.company.infrastructure.repository.company_repository import (
    CompanyRepository,
)
from app.modules.user.application.exception.user_exceptions import (
    EmailDuplicatedException,
)
from app.modules.user.application.password import (
    decode_password,
    validate_password_policy,
)
from app.modules.user.infrastructure.repository.user_repository import UserRepository
from app.modules.user.infrastructure.user import User
from app.modules.user.infrastructure.user_enums import UserRoleCode

# PostgreSQL SQLSTATE: DB 함수가 권한 검사에 실패했을 때
_PG_INSUFFICIENT_PRIVILEGE = "42501"


class ClientAccountService:
    """Glowship ADMIN 이 담당 고객사의 Client 계정을 관리하는 비즈니스 로직"""

    def __init__(
        self,
        user_repo: UserRepository,
        company_repo: CompanyRepository,
        admin_assignment_repo: AdminAssignmentRepository,
    ):
        self.user_repo = user_repo
        self.company_repo = company_repo
        self.admin_assignment_repo = admin_assignment_repo

    async def create_client_user(
        self,
        *,
        actor: CurrentUser,
        company_id: uuid.UUID,
        email: str,
        encoded_password: str,
        role_code: UserRoleCode,
        display_name: str | None,
    ) -> User:
        """
        담당 고객사에 Client 사용자(CLIENT_MEMBER·CLIENT_LEAD)를 생성한다.

        앱 계층의 검사는 사용자에게 구분된 오류 코드를 주기 위한 것이고,
        최종 강제는 DB 함수(app_admin_create_client_user)가 같은 규칙으로 다시 수행한다.

        Args:
            actor: 요청한 ADMIN (라우터에서 역할을 이미 확인함)
            company_id: 계정을 만들 고객사
            email: 생성할 계정 이메일
            encoded_password: 프론트엔드에서 btoa()로 인코딩하여 보낸 Base64 초기 비밀번호
            role_code: CLIENT_MEMBER 또는 CLIENT_LEAD (라우터 스키마가 범위를 제한함)
            display_name: 표시 이름. 비어 있으면 이메일을 쓴다

        Raises:
            InvalidPasswordFormatException: Base64 디코딩 실패 또는 비밀번호 정책 위반 (400)
            CompanyNotFoundException: 고객사 없음 (404)
            CompanyInactiveException: 비활성 고객사 (409)
            ForbiddenException: 요청자가 이 고객사에 배정되지 않았거나 계정 관리 권한이 없음 (403)
            EmailDuplicatedException: 이메일 중복 (409)
        """
        # DB 에 접근하지 않는 검사를 먼저 수행한다.
        raw_password = decode_password(encoded_password)
        validate_password_policy(raw_password)

        company = await self.company_repo.find_by_id(company_id)
        if company is None:
            raise CompanyNotFoundException()
        if company.status != CompanyStatus.ACTIVE:
            raise CompanyInactiveException()

        assignment = await self.admin_assignment_repo.find_by_admin_and_company(
            admin_user_id=actor.user_id, company_id=company_id
        )
        if assignment is None or not assignment.can_manage_client_accounts:
            raise ForbiddenException()

        # RLS 아래에서는 다른 고객사 사용자가 보이지 않아 이 검사를 통과할 수 있다.
        # 그 경우는 아래 IntegrityError(unique_violation) 경로가 같은 오류로 응답한다.
        if await self.user_repo.find_by_email(email):
            raise EmailDuplicatedException()

        password_hash = await hash_password(raw_password)

        try:
            return await self.user_repo.create_client_user(
                actor_user_id=actor.user_id,
                company_id=company_id,
                user_id=uuid.uuid7(),
                email=email,
                password_hash=password_hash,
                display_name=display_name or email,
                role_code=role_code,
            )
        except IntegrityError as e:
            raise EmailDuplicatedException() from e
        except DBAPIError as e:
            # 앱 검사와 DB 함수 검사 사이에 배정·고객사 상태가 바뀐 경우 등
            if getattr(e.orig, "pgcode", None) == _PG_INSUFFICIENT_PRIVILEGE:
                raise ForbiddenException() from e
            raise
