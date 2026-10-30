import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from app.core.schemas import CamelModel
from app.modules.user.infrastructure.user_enums import UserRoleCode, UserStatus


class ClientUserCreateRequest(CamelModel):
    """고객사 계정 생성 요청. JSON 필드는 camelCase (companyId, roleCode, displayName)."""

    company_id: uuid.UUID = Field(
        description="계정을 만들 고객사 식별자. 요청자가 배정된 고객사여야 한다.",
        examples=["1a2052c0-cbfc-523e-ac43-ff6e6d1dd9a6"],
    )
    # 이메일 규칙은 로그인 요청(auth_request_schemas.EmailLoginRequest)과 같다.
    email: str = Field(
        max_length=50,
        pattern=r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$",
        examples=["lead@client.com"],
    )
    password: str = Field(
        description="Base64 로 인코딩한 초기 비밀번호 (디코딩 결과는 영문·숫자 6~16자)",
        examples=["cGFzc3dvcmQxMjM="],
    )
    # Client 역할만 허용한다. OWNER·ADMIN 을 넣으면 요청 형식 오류(422)로 거절된다.
    role_code: Literal["CLIENT_MEMBER", "CLIENT_LEAD"] = Field(
        examples=["CLIENT_MEMBER"],
    )
    display_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=30,
        description="표시 이름. 생략하면 이메일을 사용한다.",
        examples=["홍길동"],
    )

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        """대소문자만 정규화한다. 공백은 허용하지 않으며, 위 pattern 검사가 먼저 실행되어 422 로 거절된다."""
        return v.lower()


class ClientUserCreateResponse(CamelModel):
    """생성된 고객사 계정 정보. 비밀번호 관련 값은 포함하지 않는다."""

    user_id: uuid.UUID
    company_id: uuid.UUID
    email: str
    display_name: str
    role_code: UserRoleCode
    status: UserStatus
    created_at: datetime
