import uuid

from pydantic import Field

from app.core.schemas import CamelModel
from app.modules.company.infrastructure.repository.admin_assignment_repository import (
    AssignedCompany,
)
from app.modules.user.application.me_service import MeProfile
from app.modules.user.infrastructure.user_enums import UserRoleCode


class AssignedCompanyResponse(CamelModel):
    """ADMIN·OWNER 의 담당 고객사 한 곳 (명세 4.1 GET /me assignedCompanies[], C47)"""

    company_id: uuid.UUID
    company_name: str = Field(
        description="고객사 이름 (companies.company_name)", examples=["A Beauty"]
    )
    can_manage_client_accounts: bool = Field(
        description=(
            "이 고객사의 Client 계정을 관리(생성·비밀번호 재설정)할 수 있는지. "
            "ADMIN 은 배정 행의 값, OWNER 는 모두 true"
        ),
    )

    @classmethod
    def of(cls, company: AssignedCompany) -> AssignedCompanyResponse:
        return cls(
            company_id=company.company_id,
            company_name=company.company_name,
            can_manage_client_accounts=company.can_manage_client_accounts,
        )


class MeResponse(CamelModel):
    """내 계정 정보 (명세 4.1 GET /me).
    명세의 email 은 프론트 합의로 넣지 않는다. 비밀번호 관련 값은 없다."""

    user_id: uuid.UUID
    display_name: str = Field(
        description="화면에 표시할 이름 (users.display_name). 생성 때 생략했으면 이메일과 같다",
        examples=["홍길동"],
    )
    role_code: UserRoleCode = Field(
        description="OWNER·ADMIN(Glowship 내부)·CLIENT_MEMBER(팀원)·CLIENT_LEAD(팀장)",
        examples=["CLIENT_LEAD"],
    )
    company_id: uuid.UUID | None = Field(
        description="소속 고객사. Client 역할만 값이 있고 OWNER·ADMIN 은 null (users_ck2)",
    )
    company_name: str | None = Field(
        description="소속 고객사 이름 (companies.company_name). 공통 헤더의 고객사 영역에 표시 (U7). OWNER·ADMIN 은 null",
        examples=["A Beauty"],
    )
    assigned_companies: list[AssignedCompanyResponse] = Field(
        description=(
            "담당 고객사 목록. 화면이 고객사를 고르는 근거 (C47). ADMIN 은 배정된 고객사(없으면 []), "
            "OWNER 는 전체 고객사, Client 역할은 []. ACTIVE 고객사만, 고객사명·companyId 순"
        ),
    )

    @classmethod
    def of(cls, profile: MeProfile) -> MeResponse:
        return cls(
            user_id=profile.user_id,
            display_name=profile.display_name,
            role_code=profile.role_code,
            company_id=profile.company_id,
            company_name=profile.company_name,
            assigned_companies=[
                AssignedCompanyResponse.of(c) for c in profile.assigned_companies
            ],
        )
