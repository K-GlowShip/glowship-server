import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ForbiddenException
from app.core.locks import lock_company
from app.core.rls_context import set_rls_context
from app.core.security.auth import CurrentUser
from app.modules.company.application.exception.company_exceptions import (
    CompanyInactiveException,
    CompanyNotFoundException,
)
from app.modules.company.infrastructure.company_enums import CompanyStatus
from app.modules.company.infrastructure.repository.admin_assignment_repository import (
    AdminAssignmentRepository,
)
from app.modules.company.infrastructure.repository.company_repository import (
    CompanyRepository,
)
from app.modules.decision.application.decision_clock import DecisionClock
from app.modules.operating_rule.application.exception.operating_rule_exceptions import (
    InvalidLeadTimeException,
)
from app.modules.operating_rule.infrastructure.operating_rule_enums import (
    REGION_REQUIRED_LEAD_TIME_TYPES,
)
from app.modules.operating_rule.infrastructure.repository.operating_rule_repository import (
    LeadTimeRow,
    NewLeadTime,
    NewRuleSet,
    OperatingRuleRepository,
    RuleSetRow,
)

"""
    운영 기준 서비스 (API 4 GET·API 5 POST /admin/operating-rule-sets, API 명세 v6.2 4.2절, FR-004, GLOW-153)
      - 고객사 범위는 토큰이 아니라 요청의 companyId 입니다(ADMIN 은 소속 고객사가 없음, 계정 생성 API 와 같은 방식).
        요청자가 그 고객사에 배정된 ADMIN 이어야 합니다(NFR-005). 계정 관리 권한(can_manage_client_accounts)은 보지 않습니다
        (명세 6절: 그 권한은 Client 계정 생성에만 해당).
      - 확인 순서: RLS 컨텍스트 → 배정(없으면 403) → 고객사(없으면 404, 비활성 409). 배정을 먼저 보는 이유는
        운영 앱 DB 역할에서 배정되지 않은 ADMIN 에게는 companies 행이 보이지 않아(RLS), 고객사를 먼저 보면 403 이 아니라
        404 가 되기 때문입니다. admin_assignments 정책은 app.user_id·app.company_id 만 보므로 배정 여부는 그대로 읽힙니다.
      - 저장은 고객사 잠금(L1) 안에서 version_no + 1 로 새 버전을 만들고, 기존 버전은 수정·삭제하지 않습니다.
      - 저장만으로는 기존 실행을 STALE 로 바꾸지 않습니다(serviceflow 5.2 "운영 기준 새 버전 저장만으로는 STALE 로 바꾸지 않는다").
        팀장의 최종 선택 재확인 5(selection_policy)가 활성 버전이 다르면 OPERATING_RULE_CHANGED 로 재분석을 요구합니다.
        명세 4.2 의 "저장 때 STALE" 은 제안이고 적용하지 않았습니다(11/13 결정).
      - isCurrent 기준일은 최종 선택과 같은 판단 시각(DecisionClock)의 UTC 날짜입니다. 그래야 "현재"로 보이는 버전과
        최종 선택이 비교하는 활성 버전(effective_from ≤ 선택일 중 version_no 최대)이 같습니다.
      - 저장 뒤 엔진 저장소 캐시에서 그 고객사 항목을 버립니다(on_saved). 캐시 열쇠가 (고객사, 입력 순번)이라 수집 없이
        운영 기준만 바뀌면 같은 입력 순번의 재분석이 이전 운영 기준이 든 저장소를 받기 때문입니다.
        요청 세션(get_db)은 응답 뒤에 커밋하므로, 버린 직후 다른 실행이 커밋 전 값을 다시 읽어 넣지 않게 여기서 먼저 커밋합니다.
"""


@dataclass(frozen=True)
class RuleSetDetail:
    row: RuleSetRow
    is_current: bool
    lead_times: list[LeadTimeRow]


@dataclass(frozen=True)
class CreatedRuleSet:
    operating_rule_set_id: uuid.UUID
    version_no: int


def validate_lead_times(
    lead_times: Sequence[NewLeadTime], warehouse_ids: set[uuid.UUID]
) -> None:
    """리드타임 규칙(명세 4.2 INVALID_LEAD_TIME): 유형별 도착 권역, 같은 키 중복, 이 고객사의 창고인지. DB 를 읽지 않습니다."""
    seen: set[tuple[str, uuid.UUID, str | None]] = set()
    for lead_time in lead_times:
        needs_region = lead_time.lead_time_type in REGION_REQUIRED_LEAD_TIME_TYPES
        if needs_region != (lead_time.to_region_code is not None):
            raise InvalidLeadTimeException(
                f"{lead_time.lead_time_type}: toRegionCode 는"
                f" {'필수' if needs_region else '넣을 수 없음'}"
            )
        key = (
            lead_time.lead_time_type,
            lead_time.warehouse_id,
            lead_time.to_region_code,
        )
        if key in seen:
            raise InvalidLeadTimeException(f"같은 키 중복: {key}")
        seen.add(key)
        if lead_time.warehouse_id not in warehouse_ids:
            raise InvalidLeadTimeException(
                f"이 고객사의 창고가 아님: {lead_time.warehouse_id}"
            )


class OperatingRuleService:
    def __init__(
        self,
        db: AsyncSession,
        repo: OperatingRuleRepository,
        company_repo: CompanyRepository,
        admin_assignment_repo: AdminAssignmentRepository,
        clock: DecisionClock | None = None,
        on_saved: Callable[[uuid.UUID], object] | None = None,
    ):
        self.db = db
        self.repo = repo
        self.company_repo = company_repo
        self.admin_assignment_repo = admin_assignment_repo
        self.clock = clock or DecisionClock(None, virtual=False)
        self.on_saved = on_saved

    async def _enter_company_scope(
        self, actor: CurrentUser, company_id: uuid.UUID
    ) -> None:
        """RLS 컨텍스트를 설정하고 요청자가 이 고객사에 배정된 ADMIN 인지, 고객사가 활성인지 확인한다."""
        await set_rls_context(self.db, user_id=actor.user_id, company_id=company_id)

        assignment = await self.admin_assignment_repo.find_by_admin_and_company(
            admin_user_id=actor.user_id, company_id=company_id
        )
        if assignment is None:
            raise ForbiddenException()

        company = await self.company_repo.find_by_id(company_id)
        if company is None:
            raise CompanyNotFoundException()
        if company.status != CompanyStatus.ACTIVE:
            raise CompanyInactiveException()

    async def _today(self, company_id: uuid.UUID) -> date:
        return (await self.clock.now(company_id)).astimezone(UTC).date()

    async def list_rule_sets(
        self, *, actor: CurrentUser, company_id: uuid.UUID
    ) -> list[RuleSetDetail]:
        """
        담당 고객사의 운영 기준 버전 목록 (버전 내림차순, 버전마다 리드타임 포함)

        Raises:
            ForbiddenException: 요청자가 이 고객사에 배정되지 않음 (403)
            CompanyNotFoundException: 고객사 없음 (404)
            CompanyInactiveException: 비활성 고객사 (409)
        """
        await self._enter_company_scope(actor, company_id)

        rows = await self.repo.find_rule_sets(company_id)
        lead_times: dict[uuid.UUID, list[LeadTimeRow]] = {}
        for lead_time in await self.repo.find_lead_times(company_id):
            lead_times.setdefault(lead_time.operating_rule_set_id, []).append(lead_time)

        # 현재 적용 버전 = 적용 시작일이 지난 것 중 버전이 가장 큰 것 (rows 는 버전 내림차순)
        today = await self._today(company_id)
        current_id = next(
            (r.operating_rule_set_id for r in rows if r.effective_from <= today), None
        )
        return [
            RuleSetDetail(
                row=row,
                is_current=row.operating_rule_set_id == current_id,
                lead_times=lead_times.get(row.operating_rule_set_id, []),
            )
            for row in rows
        ]

    async def create_rule_set(
        self,
        *,
        actor: CurrentUser,
        company_id: uuid.UUID,
        effective_from: date,
        safety_stock_days: int,
        sales_basis_days: int,
        allow_partial_fulfillment: bool | None,
        lead_times: Sequence[NewLeadTime],
    ) -> CreatedRuleSet:
        """
        담당 고객사의 운영 기준 새 버전을 저장한다. 작성자는 요청한 ADMIN 이다.

        Raises:
            ForbiddenException: 요청자가 이 고객사에 배정되지 않음 (403)
            CompanyNotFoundException: 고객사 없음 (404)
            CompanyInactiveException: 비활성 고객사 (409)
            InvalidLeadTimeException: 유형별 toRegionCode 규칙 위반, 같은 키 중복, 이 고객사의 창고가 아님 (400)
        """
        await self._enter_company_scope(actor, company_id)
        validate_lead_times(lead_times, await self.repo.find_warehouse_ids(company_id))

        # L1: 같은 고객사의 동시 저장이 같은 버전 번호를 받지 않게, 분석 입력 확정과 겹치지 않게 한다(serviceflow 5.2)
        await lock_company(self.db, company_id)
        new = NewRuleSet(
            operating_rule_set_id=uuid.uuid7(),
            version_no=await self.repo.find_max_version_no(company_id) + 1,
            user_id=actor.user_id,
            effective_from=effective_from,
            safety_stock_days=safety_stock_days,
            sales_basis_days=sales_basis_days,
            allow_partial_fulfillment=allow_partial_fulfillment,
            created_at=await self.clock.now(company_id),
            lead_times=tuple(lead_times),
        )
        await self.repo.insert_rule_set(company_id, new)
        await self.db.commit()
        if self.on_saved is not None:
            self.on_saved(company_id)
        return CreatedRuleSet(
            operating_rule_set_id=new.operating_rule_set_id, version_no=new.version_no
        )
