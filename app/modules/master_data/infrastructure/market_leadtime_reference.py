from decimal import Decimal

from sqlalchemy import UniqueConstraint
from sqlalchemy.orm import Mapped

from app.core.orm import Base, uuid_pk


class MarketLeadtimeReference(Base):
    """미국 수입 시장 리드타임 기준값 (고객사 공용, RLS 대상 아님)"""

    __tablename__ = "market_leadtime_references"
    __table_args__ = (
        UniqueConstraint(
            "month",
            "us_port_code",
            "hs10",
            "transport_mode",
            name="market_leadtime_references_uk1",
        ),
    )

    market_leadtime_reference_id: Mapped[uuid_pk]
    month: Mapped[str]
    us_port_code: Mapped[str]
    us_port_name: Mapped[str]
    hs10: Mapped[str]
    transport_mode: Mapped[str]
    total_leadtime_p50: Mapped[Decimal]
    total_leadtime_p90: Mapped[Decimal]
    confidence_leadtime: Mapped[str]
    source_file: Mapped[str]
