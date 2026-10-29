import uuid

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.orm import Mapped

from app.core.orm import Base, uuid_pk


class OperatingLeadTime(Base):
    """운영 규칙 버전별 창고 리드타임 (배송·항공 운송·입출고 처리)"""

    __tablename__ = "operating_lead_times"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "operating_lead_time_id", name="operating_lead_times_uk1"
        ),
        UniqueConstraint(
            "operating_rule_set_id",
            "lead_time_type",
            "warehouse_id",
            "to_region_code",
            name="operating_lead_times_uk2",
        ),
        CheckConstraint(
            "lead_time_type IN"
            " ('CUSTOMER_DELIVERY','AIR_TRANSIT','INBOUND_HANDLING','OUTBOUND_HANDLING')",
            name="operating_lead_times_ck1",
        ),
        CheckConstraint(
            "day_basis IN ('CALENDAR_DAY','BUSINESS_DAY')",
            name="operating_lead_times_ck2",
        ),
        CheckConstraint("days >= 0", name="operating_lead_times_ck3"),
        CheckConstraint(
            "(lead_time_type IN ('CUSTOMER_DELIVERY','AIR_TRANSIT'))"
            " = (to_region_code IS NOT NULL)",
            name="operating_lead_times_ck4",
        ),
        ForeignKeyConstraint(
            ["company_id", "operating_rule_set_id"],
            [
                "operating_rule_sets.company_id",
                "operating_rule_sets.operating_rule_set_id",
            ],
            name="fk_operating_lead_times__operating_rule_set_id",
        ),
        ForeignKeyConstraint(
            ["company_id", "warehouse_id"],
            ["warehouses.company_id", "warehouses.warehouse_id"],
            name="fk_operating_lead_times__warehouse_id",
        ),
    )

    operating_lead_time_id: Mapped[uuid_pk]
    company_id: Mapped[uuid.UUID]
    operating_rule_set_id: Mapped[uuid.UUID]
    lead_time_type: Mapped[str]
    warehouse_id: Mapped[uuid.UUID]
    to_region_code: Mapped[str | None]
    days: Mapped[int]
    day_basis: Mapped[str]
