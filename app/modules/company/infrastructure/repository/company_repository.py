import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.company.infrastructure.company import Company


class CompanyRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # Read
    async def find_by_id(self, company_id: uuid.UUID) -> Company | None:
        """고객사 식별자로 조회"""
        stmt = select(Company).where(Company.company_id == company_id)
        return await self.db.scalar(stmt)
