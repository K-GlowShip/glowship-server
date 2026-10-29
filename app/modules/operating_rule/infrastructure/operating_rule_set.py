import uuid
from datetime import date, datetime

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.orm import Mapped

from app.core.orm import Base, uuid_pk


class OperatingRuleSet(Base):
    """고객사 운영 규칙 버전 (안전재고 일수, 판매 기준 일수 등)"""

    __tablename__ = "operating_rule_sets"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "operating_rule_set_id", name="operating_rule_sets_uk1"
        ),
        UniqueConstraint("company_id", "version_no", name="operating_rule_sets_uk2"),
        CheckConstraint("safety_stock_days > 0", name="operating_rule_sets_ck1"),
        CheckConstraint("sales_basis_days > 0", name="operating_rule_sets_ck2"),
        ForeignKeyConstraint(
            ["company_id"],
            ["companies.company_id"],
            name="fk_operating_rule_sets__company_id",
        ),
        ForeignKeyConstraint(
            ["user_id"], ["users.user_id"], name="fk_operating_rule_sets__user_id"
        ),
    )

    operating_rule_set_id: Mapped[uuid_pk]
    company_id: Mapped[uuid.UUID]
    version_no: Mapped[int]
    user_id: Mapped[uuid.UUID]
    effective_from: Mapped[date]
    safety_stock_days: Mapped[int]
    sales_basis_days: Mapped[int]
    allow_partial_fulfillment: Mapped[bool | None]
    created_at: Mapped[datetime]
