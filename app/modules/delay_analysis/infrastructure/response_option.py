import uuid
from datetime import date

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.orm import Base, uuid_pk


class ResponseOption(Base):
    """분석 실행별 대응안 (계획 유지, 출항 전 항공 전환, 한국 항공 보충, 동부 창고 출고)"""

    __tablename__ = "response_options"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "response_option_id", name="response_options_uk1"
        ),
        UniqueConstraint("analysis_run_id", "option_type", name="response_options_uk2"),
        UniqueConstraint(
            "company_id",
            "analysis_run_id",
            "response_option_id",
            name="response_options_uk3",
        ),
        CheckConstraint(
            "option_type IN"
            " ('KEEP_PLAN','PRE_DEPARTURE_AIR','KR_AIR_REPLENISH','EAST_FULFILL')",
            name="response_options_ck1",
        ),
        CheckConstraint(
            "feasibility_status IN ('FEASIBLE','NOT_FEASIBLE','NEEDS_CHECK')",
            name="response_options_ck2",
        ),
        CheckConstraint(
            "feasibility_status = 'FEASIBLE' OR reason IS NOT NULL",
            name="response_options_ck3",
        ),
        CheckConstraint(
            "NOT is_recommended OR feasibility_status = 'FEASIBLE'",
            name="response_options_ck4",
        ),
        ForeignKeyConstraint(
            ["company_id", "analysis_run_id"],
            ["analysis_runs.company_id", "analysis_runs.analysis_run_id"],
            name="fk_response_options__analysis_run_id",
        ),
        Index(
            "response_options_is_recommended_uq",
            "analysis_run_id",
            unique=True,
            postgresql_where=text("is_recommended"),
        ),
    )

    response_option_id: Mapped[uuid_pk]
    company_id: Mapped[uuid.UUID]
    analysis_run_id: Mapped[uuid.UUID]
    option_type: Mapped[str]
    feasibility_status: Mapped[str]
    reason: Mapped[str | None]
    required_quantity: Mapped[int]
    securable_quantity: Mapped[int | None]
    expected_delivery_date: Mapped[date | None]
    fully_recovered_orders: Mapped[int]
    partially_recovered_orders: Mapped[int]
    not_recovered_orders: Mapped[int]
    is_recommended: Mapped[bool] = mapped_column(default=False, server_default=false())
