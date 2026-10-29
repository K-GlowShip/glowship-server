import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.orm import Mapped

from app.core.orm import Base, uuid_pk


class OptionOrderOutcome(Base):
    """대응안별 위험 주문 회복 결과 (전량·부분·미회복, 공급원 창고)"""

    __tablename__ = "option_order_outcomes"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "option_order_outcome_id", name="option_order_outcomes_uk1"
        ),
        UniqueConstraint(
            "response_option_id",
            "at_risk_order_id",
            name="option_order_outcomes_uk2",
        ),
        CheckConstraint(
            "outcome IN ('FULL','PARTIAL','NOT_RECOVERED')",
            name="option_order_outcomes_ck1",
        ),
        CheckConstraint("recovered_quantity >= 0", name="option_order_outcomes_ck2"),
        CheckConstraint(
            "(outcome = 'NOT_RECOVERED') = (recovered_quantity = 0)",
            name="option_order_outcomes_ck3",
        ),
        CheckConstraint(
            "allocated_quantity >= recovered_quantity",
            name="option_order_outcomes_ck4",
        ),
        CheckConstraint(
            "(allocated_quantity > 0) = (source_warehouse_id IS NOT NULL)",
            name="option_order_outcomes_ck5",
        ),
        CheckConstraint(
            "(source_warehouse_id IS NULL) = (supply_source IS NULL)",
            name="option_order_outcomes_ck6",
        ),
        CheckConstraint(
            "supply_source IS NULL OR supply_source IN"
            " ('EXISTING_STOCK','EAST_WAREHOUSE','KR_AIR','PRE_DEPARTURE_AIR')",
            name="option_order_outcomes_ck7",
        ),
        ForeignKeyConstraint(
            ["company_id", "analysis_run_id", "response_option_id"],
            [
                "response_options.company_id",
                "response_options.analysis_run_id",
                "response_options.response_option_id",
            ],
            name="fk_option_order_outcomes__response_option_id",
        ),
        ForeignKeyConstraint(
            ["company_id", "analysis_run_id", "at_risk_order_id"],
            [
                "at_risk_orders.company_id",
                "at_risk_orders.analysis_run_id",
                "at_risk_orders.at_risk_order_id",
            ],
            name="fk_option_order_outcomes__at_risk_order_id",
        ),
        ForeignKeyConstraint(
            ["company_id", "source_warehouse_id"],
            ["warehouses.company_id", "warehouses.warehouse_id"],
            name="fk_option_order_outcomes__source_warehouse_id",
        ),
    )

    option_order_outcome_id: Mapped[uuid_pk]
    company_id: Mapped[uuid.UUID]
    analysis_run_id: Mapped[uuid.UUID]
    response_option_id: Mapped[uuid.UUID]
    at_risk_order_id: Mapped[uuid.UUID]
    outcome: Mapped[str]
    expected_delivery_at: Mapped[datetime | None]
    recovered_quantity: Mapped[int]
    source_warehouse_id: Mapped[uuid.UUID | None]
    supply_source: Mapped[str | None]
    allocated_quantity: Mapped[int]
