from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.modules.company.infrastructure.repository.admin_assignment_repository import (
    AdminAssignmentRepository,
)
from app.modules.company.infrastructure.repository.company_repository import (
    CompanyRepository,
)

"""
    company 모듈 의존성 조립
      - 요청 단위 DB 세션(get_db)을 받아 리포지토리를 만들어 다른 모듈의 서비스·라우터에 주입합니다.
"""


def get_company_repository(db: AsyncSession = Depends(get_db)) -> CompanyRepository:
    return CompanyRepository(db)


def get_admin_assignment_repository(
    db: AsyncSession = Depends(get_db),
) -> AdminAssignmentRepository:
    return AdminAssignmentRepository(db)
