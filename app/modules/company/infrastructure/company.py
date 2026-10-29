from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.orm import Mapped

from app.core.orm import Base, uuid_pk


class Company(Base):
    """고객사 (테넌트 단위)"""

    __tablename__ = "companies"
    __table_args__ = (
        UniqueConstraint("company_code", name="companies_uk1"),
        CheckConstraint("status IN ('ACTIVE','INACTIVE')", name="companies_ck1"),
    )

    company_id: Mapped[uuid_pk]
    company_code: Mapped[str]
    company_name: Mapped[str]
    status: Mapped[str]
