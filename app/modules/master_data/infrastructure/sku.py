import uuid

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.orm import Mapped

from app.core.orm import Base, uuid_pk


class Sku(Base):
    """고객사 상품(SKU) 마스터"""

    __tablename__ = "skus"
    __table_args__ = (
        UniqueConstraint("company_id", "sku_id", name="skus_uk1"),
        UniqueConstraint("company_id", "sku_code", name="skus_uk2"),
        CheckConstraint(
            "popularity_tier IN ('HIT','REGULAR','LONG_TAIL')", name="skus_ck1"
        ),
        CheckConstraint("units_per_case > 0", name="skus_ck2"),
        CheckConstraint("status IN ('ACTIVE','INACTIVE')", name="skus_ck3"),
        ForeignKeyConstraint(
            ["company_id"], ["companies.company_id"], name="fk_skus__company_id"
        ),
    )

    sku_id: Mapped[uuid_pk]
    company_id: Mapped[uuid.UUID]
    sku_code: Mapped[str]
    product_name: Mapped[str]
    brand_name: Mapped[str]
    hs10: Mapped[str]
    category: Mapped[str]
    subcategory: Mapped[str]
    popularity_tier: Mapped[str]
    unit_code: Mapped[str]
    units_per_case: Mapped[int]
    emergency_air_eligible: Mapped[bool]
    status: Mapped[str]
