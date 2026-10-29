import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.orm import Mapped

from app.core.orm import Base, uuid_pk


class AtRiskOrder(Base):
    """분석 실행별 납기 위험 주문 상세"""

    __tablename__ = "at_risk_orders"
    __table_args__ = (
        UniqueConstraint("company_id", "at_risk_order_id", name="at_risk_orders_uk1"),
        UniqueConstraint("analysis_run_id", "order_line_id", name="at_risk_orders_uk2"),
        UniqueConstraint(
            "company_id",
            "analysis_run_id",
            "at_risk_order_id",
            name="at_risk_orders_uk3",
        ),
        CheckConstraint("open_quantity > 0", name="at_risk_orders_ck1"),
        CheckConstraint(
            "expected_delivery_at > promised_delivery_at", name="at_risk_orders_ck2"
        ),
        ForeignKeyConstraint(
            ["company_id", "analysis_run_id"],
            ["analysis_runs.company_id", "analysis_runs.analysis_run_id"],
            name="fk_at_risk_orders__analysis_run_id",
        ),
        ForeignKeyConstraint(
            ["company_id", "order_line_id"],
            ["order_lines.company_id", "order_lines.order_line_id"],
            name="fk_at_risk_orders__order_line_id",
        ),
    )

    at_risk_order_id: Mapped[uuid_pk]
    company_id: Mapped[uuid.UUID]
    analysis_run_id: Mapped[uuid.UUID]
    order_line_id: Mapped[uuid.UUID]
    open_quantity: Mapped[int]
    promised_delivery_at: Mapped[datetime]
    expected_delivery_at: Mapped[datetime]
    risk_reason: Mapped[str]
