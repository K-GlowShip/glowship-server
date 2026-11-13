import uuid
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.master_data.infrastructure.warehouse import Warehouse
from app.modules.operating_rule.infrastructure.operating_lead_time import (
    OperatingLeadTime,
)
from app.modules.operating_rule.infrastructure.operating_rule_set import (
    OperatingRuleSet,
)
from app.modules.user.infrastructure.user import User

"""
    운영 기준 리포지토리 (API 4·5 /admin/operating-rule-sets, API 명세 v6.2 4.2절)
      - RLS 에만 기대지 않고 모든 조회 조건에 company_id 를 넣습니다(로컬은 superuser 라 RLS 가 적용되지 않음).
      - RLS 컨텍스트는 서비스가 첫 조회 전에 같은 트랜잭션에 설정해 둡니다.
      - 작성자는 LEFT JOIN 입니다. 운영 앱 DB 역할에서는 users 정책(users_select_tenant_or_self)이 그 고객사 사용자와
        요청자 본인 행만 보여 주므로, 다른 ADMIN 이 만든 버전의 작성자는 보이지 않아 None 입니다.
"""


@dataclass(frozen=True)
class RuleSetRow:
    operating_rule_set_id: uuid.UUID
    version_no: int
    effective_from: date
    safety_stock_days: int
    sales_basis_days: int
    allow_partial_fulfillment: bool | None
    created_at: datetime
    author_user_id: uuid.UUID
    author_display_name: str | None  # 작성자 행이 보이지 않으면 None
    author_role_code: str | None


@dataclass(frozen=True)
class LeadTimeRow:
    operating_rule_set_id: uuid.UUID
    lead_time_type: str
    warehouse_id: uuid.UUID
    warehouse_code: str
    warehouse_region_code: str
    to_region_code: str | None
    days: int
    day_basis: str


@dataclass(frozen=True)
class NewLeadTime:
    lead_time_type: str
    warehouse_id: uuid.UUID
    to_region_code: str | None
    days: int
    day_basis: str


@dataclass(frozen=True)
class NewRuleSet:
    operating_rule_set_id: uuid.UUID
    version_no: int
    user_id: uuid.UUID
    effective_from: date
    safety_stock_days: int
    sales_basis_days: int
    allow_partial_fulfillment: bool | None
    created_at: datetime
    lead_times: tuple[NewLeadTime, ...]


class OperatingRuleRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # Read
    async def find_rule_sets(self, company_id: uuid.UUID) -> list[RuleSetRow]:
        """고객사의 운영 기준 버전 전체. 버전 내림차순(버전은 고객사 안에서 겹치지 않음, operating_rule_sets_uk2)"""
        stmt = (
            select(OperatingRuleSet, User.display_name, User.role_code)
            .outerjoin(User, User.user_id == OperatingRuleSet.user_id)
            .where(OperatingRuleSet.company_id == company_id)
            .order_by(OperatingRuleSet.version_no.desc())
        )
        return [
            RuleSetRow(
                operating_rule_set_id=rule_set.operating_rule_set_id,
                version_no=rule_set.version_no,
                effective_from=rule_set.effective_from,
                safety_stock_days=rule_set.safety_stock_days,
                sales_basis_days=rule_set.sales_basis_days,
                allow_partial_fulfillment=rule_set.allow_partial_fulfillment,
                created_at=rule_set.created_at,
                author_user_id=rule_set.user_id,
                author_display_name=display_name,
                author_role_code=None if role_code is None else role_code.value,
            )
            for rule_set, display_name, role_code in await self.db.execute(stmt)
        ]

    async def find_lead_times(self, company_id: uuid.UUID) -> list[LeadTimeRow]:
        """고객사의 모든 버전 리드타임. 유형 → 창고 코드 → 도착 권역 순서(버전별 묶기는 서비스가 한다)"""
        stmt = (
            select(OperatingLeadTime, Warehouse.warehouse_code, Warehouse.region_code)
            .join(
                Warehouse,
                (Warehouse.company_id == OperatingLeadTime.company_id)
                & (Warehouse.warehouse_id == OperatingLeadTime.warehouse_id),
            )
            .where(OperatingLeadTime.company_id == company_id)
            .order_by(
                OperatingLeadTime.lead_time_type,
                Warehouse.warehouse_code,
                OperatingLeadTime.to_region_code.nulls_first(),
            )
        )
        return [
            LeadTimeRow(
                operating_rule_set_id=lead_time.operating_rule_set_id,
                lead_time_type=lead_time.lead_time_type,
                warehouse_id=lead_time.warehouse_id,
                warehouse_code=warehouse_code,
                warehouse_region_code=region_code,
                to_region_code=lead_time.to_region_code,
                days=lead_time.days,
                day_basis=lead_time.day_basis,
            )
            for lead_time, warehouse_code, region_code in await self.db.execute(stmt)
        ]

    async def find_max_version_no(self, company_id: uuid.UUID) -> int:
        """고객사의 가장 큰 버전 번호. 버전이 없으면 0. 고객사 잠금(L1) 안에서 불러야 한다"""
        stmt = select(func.max(OperatingRuleSet.version_no)).where(
            OperatingRuleSet.company_id == company_id
        )
        return await self.db.scalar(stmt) or 0

    async def find_warehouse_ids(self, company_id: uuid.UUID) -> set[uuid.UUID]:
        """고객사의 창고 ID 전체 (리드타임의 창고가 이 고객사 것인지 확인)"""
        stmt = select(Warehouse.warehouse_id).where(Warehouse.company_id == company_id)
        return set(await self.db.scalars(stmt))

    # Create
    async def insert_rule_set(self, company_id: uuid.UUID, new: NewRuleSet) -> None:
        """운영 기준 한 버전과 그 리드타임을 넣는다. 기존 버전은 건드리지 않는다(명세 4.2 "수정·삭제하지 않음")"""
        self.db.add(
            OperatingRuleSet(
                operating_rule_set_id=new.operating_rule_set_id,
                company_id=company_id,
                version_no=new.version_no,
                user_id=new.user_id,
                effective_from=new.effective_from,
                safety_stock_days=new.safety_stock_days,
                sales_basis_days=new.sales_basis_days,
                allow_partial_fulfillment=new.allow_partial_fulfillment,
                created_at=new.created_at,
            )
        )
        # 리드타임은 운영 기준 버전을 복합 FK 로 참조하므로 버전 행을 먼저 넣는다
        await self.db.flush()
        self.db.add_all(
            OperatingLeadTime(
                operating_lead_time_id=uuid.uuid7(),
                company_id=company_id,
                operating_rule_set_id=new.operating_rule_set_id,
                lead_time_type=lead_time.lead_time_type,
                warehouse_id=lead_time.warehouse_id,
                to_region_code=lead_time.to_region_code,
                days=lead_time.days,
                day_basis=lead_time.day_basis,
            )
            for lead_time in new.lead_times
        )
        await self.db.flush()
