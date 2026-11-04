import uuid
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.data_sync.application.validation.blocking_issues import OpenIssue
from app.modules.data_sync.infrastructure.data_sync_enums import (
    IssueEntityType,
    SourceDomain,
)
from app.modules.data_sync.infrastructure.data_validation_issue import (
    DataValidationIssue,
)
from app.modules.master_data.infrastructure.external_entity_mapping import (
    ExternalEntityMapping,
)

"""
    검증 이슈 조회 (GLOW-15, 분석 보류 판정용)
      - 모든 조회는 고객사(company_id)로 거릅니다. 로컬 superuser 접속은 RLS 를 우회하므로 조건을 직접 겁니다.
      - 시각 조건은 "그 시각에 보였던 상태"입니다: 감지가 그 시각 이하이고 해소가 없거나 그 시각보다 뒤인 이슈,
        매핑 시각이 없거나 그 시각 이하인 MAPPED 매핑 (검증 엔진 snapshot.py 와 같음).
"""


class ValidationIssueRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def find_open_inventory_issues(
        self, company_id: uuid.UUID, at: datetime
    ) -> list[OpenIssue]:
        """at 시각에 열려 있던 분석 차단 재고 이슈. 감지 시각 순, 같으면 키 순."""
        rows = await self.db.execute(
            select(
                DataValidationIssue.source_record_key, DataValidationIssue.error_code
            )
            .where(
                DataValidationIssue.company_id == company_id,
                DataValidationIssue.source_entity_type
                == IssueEntityType.INVENTORY.value,
                DataValidationIssue.blocks_analysis.is_(True),
                DataValidationIssue.detected_at <= at,
                or_(
                    DataValidationIssue.resolved_at.is_(None),
                    DataValidationIssue.resolved_at > at,
                ),
            )
            .order_by(
                DataValidationIssue.detected_at, DataValidationIssue.source_record_key
            )
        )
        return [OpenIssue(row.source_record_key, row.error_code) for row in rows]

    async def inventory_warehouse_mappings(
        self, company_id: uuid.UUID, at: datetime
    ) -> dict[str, uuid.UUID]:
        """at 시각까지 MAPPED 된 재고(WMS) 창고 외부 코드 → 내부 창고 ID"""
        rows = await self.db.execute(
            select(
                ExternalEntityMapping.external_code, ExternalEntityMapping.warehouse_id
            ).where(
                ExternalEntityMapping.company_id == company_id,
                ExternalEntityMapping.source_domain == SourceDomain.INVENTORY.value,
                ExternalEntityMapping.entity_type == "WAREHOUSE",
                ExternalEntityMapping.mapping_status == "MAPPED",
                ExternalEntityMapping.warehouse_id.is_not(None),
                or_(
                    ExternalEntityMapping.mapped_at.is_(None),
                    ExternalEntityMapping.mapped_at <= at,
                ),
            )
        )
        return {row.external_code: row.warehouse_id for row in rows}
