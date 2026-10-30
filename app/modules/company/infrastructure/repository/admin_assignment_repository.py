import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.company.infrastructure.admin_assignment import AdminAssignment


class AdminAssignmentRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # Read
    async def find_by_admin_and_company(
        self, admin_user_id: uuid.UUID, company_id: uuid.UUID
    ) -> AdminAssignment | None:
        """ADMIN 사용자의 특정 고객사 배정 조회 (admin_assignments_uk2 로 최대 1건)"""
        stmt = select(AdminAssignment).where(
            AdminAssignment.admin_user_id == admin_user_id,
            AdminAssignment.company_id == company_id,
        )
        return await self.db.scalar(stmt)
