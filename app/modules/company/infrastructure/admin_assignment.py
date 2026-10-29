import uuid
from datetime import datetime

from sqlalchemy import ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.orm import Mapped

from app.core.orm import Base, uuid_pk


class AdminAssignment(Base):
    """GlowShip ADMIN 사용자의 담당 고객사 배정"""

    __tablename__ = "admin_assignments"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "admin_assignment_id", name="admin_assignments_uk1"
        ),
        UniqueConstraint("admin_user_id", "company_id", name="admin_assignments_uk2"),
        ForeignKeyConstraint(
            ["admin_user_id"],
            ["users.user_id"],
            name="fk_admin_assignments__admin_user_id",
        ),
        ForeignKeyConstraint(
            ["company_id"],
            ["companies.company_id"],
            name="fk_admin_assignments__company_id",
        ),
        ForeignKeyConstraint(
            ["granted_by_user_id"],
            ["users.user_id"],
            name="fk_admin_assignments__granted_by_user_id",
        ),
    )

    admin_assignment_id: Mapped[uuid_pk]
    admin_user_id: Mapped[uuid.UUID]
    company_id: Mapped[uuid.UUID]
    granted_by_user_id: Mapped[uuid.UUID]
    can_manage_client_accounts: Mapped[bool]
    granted_at: Mapped[datetime]
