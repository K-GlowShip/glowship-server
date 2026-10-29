import uuid

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.orm import Mapped

from app.core.orm import Base, uuid_pk


class ShipmentLine(Base):
    """선적 상세 (SKU별 선적 수량)"""

    __tablename__ = "shipment_lines"
    __table_args__ = (
        UniqueConstraint("company_id", "shipment_line_id", name="shipment_lines_uk1"),
        UniqueConstraint("shipment_id", "line_no", name="shipment_lines_uk2"),
        CheckConstraint("shipped_quantity > 0", name="shipment_lines_ck1"),
        ForeignKeyConstraint(
            ["company_id", "shipment_id"],
            ["shipments.company_id", "shipments.shipment_id"],
            name="fk_shipment_lines__shipment_id",
        ),
        ForeignKeyConstraint(
            ["company_id", "sku_id"],
            ["skus.company_id", "skus.sku_id"],
            name="fk_shipment_lines__sku_id",
        ),
    )

    shipment_line_id: Mapped[uuid_pk]
    company_id: Mapped[uuid.UUID]
    shipment_id: Mapped[uuid.UUID]
    line_no: Mapped[int]
    sku_id: Mapped[uuid.UUID]
    shipped_quantity: Mapped[int]
