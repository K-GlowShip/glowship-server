import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.orm import Mapped

from app.core.orm import Base, uuid_pk


class ExternalEntityMapping(Base):
    """외부 시스템 코드 → 내부 SKU·창고 매핑"""

    __tablename__ = "external_entity_mappings"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "external_entity_mapping_id",
            name="external_entity_mappings_uk1",
        ),
        UniqueConstraint(
            "company_id",
            "source_domain",
            "entity_type",
            "external_code",
            name="external_entity_mappings_uk2",
        ),
        CheckConstraint(
            "source_domain IN ('PRODUCT','WAREHOUSE','ORDER','SHIPMENT','INVENTORY')",
            name="external_entity_mappings_ck1",
        ),
        CheckConstraint(
            "entity_type IN ('SKU','WAREHOUSE')", name="external_entity_mappings_ck2"
        ),
        CheckConstraint(
            "mapping_status IN ('PENDING','MAPPED','UNMAPPED')",
            name="external_entity_mappings_ck3",
        ),
        CheckConstraint(
            "mapping_status <> 'MAPPED'"
            " OR (entity_type = 'SKU' AND sku_id IS NOT NULL AND warehouse_id IS NULL)"
            " OR (entity_type = 'WAREHOUSE' AND warehouse_id IS NOT NULL AND sku_id IS NULL)",
            name="external_entity_mappings_ck4",
        ),
        CheckConstraint(
            "mapping_status = 'MAPPED' OR (sku_id IS NULL AND warehouse_id IS NULL)",
            name="external_entity_mappings_ck5",
        ),
        CheckConstraint(
            "(mapping_status = 'MAPPED') = (mapped_at IS NOT NULL)",
            name="external_entity_mappings_ck6",
        ),
        ForeignKeyConstraint(
            ["company_id"],
            ["companies.company_id"],
            name="fk_external_entity_mappings__company_id",
        ),
        ForeignKeyConstraint(
            ["company_id", "sku_id"],
            ["skus.company_id", "skus.sku_id"],
            name="fk_external_entity_mappings__sku_id",
        ),
        ForeignKeyConstraint(
            ["company_id", "warehouse_id"],
            ["warehouses.company_id", "warehouses.warehouse_id"],
            name="fk_external_entity_mappings__warehouse_id",
        ),
        ForeignKeyConstraint(
            ["user_id"],
            ["users.user_id"],
            name="fk_external_entity_mappings__user_id",
        ),
    )

    external_entity_mapping_id: Mapped[uuid_pk]
    company_id: Mapped[uuid.UUID]
    source_domain: Mapped[str]
    entity_type: Mapped[str]
    external_code: Mapped[str]
    sku_id: Mapped[uuid.UUID | None]
    warehouse_id: Mapped[uuid.UUID | None]
    mapping_status: Mapped[str]
    user_id: Mapped[uuid.UUID | None]
    mapped_at: Mapped[datetime | None]
