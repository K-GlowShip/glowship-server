import uuid
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.company.infrastructure.admin_assignment import AdminAssignment

# 담당 고객사 목록은 고객사를 고르기 전(app.company_id 없음)에 읽는다. 앱 DB 역할의 admin_assignments·companies 정책은
# app.company_id 인 1건만 보여 주므로 select 로는 0행이고, OWNER 는 배정 행이 없다.
# 소유자 역할의 SECURITY DEFINER 함수(마이그레이션 0011)가 app.user_id 의 역할을 보고 목록을 돌려준다.
_LIST_ASSIGNED_COMPANIES = text(
    "SELECT company_id, company_name, can_manage_client_accounts"
    " FROM app_me_list_assigned_companies()"
)


@dataclass(frozen=True)
class AssignedCompany:
    """ADMIN·OWNER 가 담당하는 고객사 한 곳 (app_me_list_assigned_companies 결과)"""

    company_id: uuid.UUID
    company_name: str
    can_manage_client_accounts: bool


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

    async def find_assigned_companies(self) -> list[AssignedCompany]:
        """
        요청자의 담당 고객사 목록. ADMIN 은 배정된 고객사, OWNER 는 전체 고객사, 그 밖은 빈 목록이다.

        누구의 목록인지는 인자가 아니라 같은 트랜잭션의 RLS 컨텍스트(app.user_id)로 정해진다.
        ACTIVE 고객사만, 고객사명·company_id 순으로 돌려준다.
        """
        rows = await self.db.execute(_LIST_ASSIGNED_COMPANIES)
        return [
            AssignedCompany(
                company_id=row.company_id,
                company_name=row.company_name,
                can_manage_client_accounts=row.can_manage_client_accounts,
            )
            for row in rows
        ]
