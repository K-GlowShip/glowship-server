import uuid

from pydantic import Field

from app.core.schemas import CamelModel
from app.modules.user.application.me_service import MeProfile
from app.modules.user.infrastructure.user_enums import UserRoleCode


class MeResponse(CamelModel):
    """내 계정 정보 (명세 4.1 GET /me).
    명세의 email 과 ADMIN 의 assignedCompanies[] 는 프론트 합의로 넣지 않는다. 비밀번호 관련 값은 없다."""

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

    @classmethod
    def of(cls, profile: MeProfile) -> MeResponse:
        return cls(
            user_id=profile.user_id,
            display_name=profile.display_name,
            role_code=profile.role_code,
            company_id=profile.company_id,
            company_name=profile.company_name,
        )
