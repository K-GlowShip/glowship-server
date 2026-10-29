import uuid

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.orm import Mapped

from app.core.orm import Base, uuid_pk


class Warehouse(Base):
    """한국 출고지·미국 3PL 창고"""

    __tablename__ = "warehouses"
    __table_args__ = (
        UniqueConstraint("company_id", "warehouse_id", name="warehouses_uk1"),
        UniqueConstraint("company_id", "warehouse_code", name="warehouses_uk2"),
        CheckConstraint(
            "warehouse_type IN ('KR_ORIGIN','US_3PL')", name="warehouses_ck1"
        ),
        CheckConstraint("region_code IN ('KR','WEST','EAST')", name="warehouses_ck2"),
        ForeignKeyConstraint(
            ["company_id"], ["companies.company_id"], name="fk_warehouses__company_id"
        ),
    )

    warehouse_id: Mapped[uuid_pk]
    company_id: Mapped[uuid.UUID]
    warehouse_code: Mapped[str]
    warehouse_name: Mapped[str]
    warehouse_type: Mapped[str]
    region_code: Mapped[str]
