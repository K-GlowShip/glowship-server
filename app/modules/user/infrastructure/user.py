import uuid

from sqlalchemy import (
    UUID,
    CheckConstraint,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
)
from sqlalchemy import (
    Enum as SQLEnum,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.orm import Base, created_at, updated_at
from app.modules.user.infrastructure.user_enums import (
    UserRoleCode,
    UserStatus,
)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("company_id", "user_id", name="users_uk1"),
        UniqueConstraint("email", name="users_uk2"),
        # Client 역할만 회사에 소속되고, OWNER·ADMIN은 company_id가 비어 있어야 한다.
        CheckConstraint(
            "(role_code IN ('CLIENT_MEMBER','CLIENT_LEAD')) = (company_id IS NOT NULL)",
            name="users_ck2",
        ),
        ForeignKeyConstraint(
            ["company_id"],
            ["companies.company_id"],
            name="fk_users__company_id",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid7,
        comment="회원 UUIDv7 식별자",
    )

    # Glowship 내부 운영 관리자인 경우, null이 들어가는 제약조건 존재
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        comment="소속 회사 식별자",
    )

    role_code: Mapped[UserRoleCode] = mapped_column(
        SQLEnum(
            UserRoleCode,
            name="role_code",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
        ),
        nullable=False,
        comment="회원 권한",
    )

    status: Mapped[UserStatus] = mapped_column(
        SQLEnum(
            UserStatus,
            name="status",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
        ),
        nullable=False,
        comment="계정 활성화 여부 상태",
    )

    email: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="회원 이메일",
    )

    password_hash: Mapped[str] = mapped_column(
        String(80),
        nullable=False,
        comment="회원 비밀번호 해시값",
    )

    display_name: Mapped[str] = mapped_column(
        String(30),
        default=lambda context: context.get_current_parameters().get("email"),
        nullable=False,
        comment="회원 표시 이름",
    )

    created_at: Mapped[created_at]
    updated_at: Mapped[updated_at]
