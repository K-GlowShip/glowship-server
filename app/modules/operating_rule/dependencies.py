from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.modules.company.dependencies import (
    get_admin_assignment_repository,
    get_company_repository,
)
from app.modules.company.infrastructure.repository.admin_assignment_repository import (
    AdminAssignmentRepository,
)
from app.modules.company.infrastructure.repository.company_repository import (
    CompanyRepository,
)
from app.modules.decision.application.decision_clock import DecisionClock
from app.modules.decision.dependencies import get_decision_clock
from app.modules.delay_analysis.infrastructure.engine.engine_store_cache import (
    engine_store_cache,
)
from app.modules.operating_rule.application.operating_rule_service import (
    OperatingRuleService,
)
from app.modules.operating_rule.infrastructure.repository.operating_rule_repository import (
    OperatingRuleRepository,
)

"""
    operating_rule 모듈 의존성 조립
      - 요청 단위 DB 세션(get_db)을 받아 리포지토리와 서비스를 만들어 라우터에 주입합니다.
      - 판단 시각(DecisionClock)은 최종 선택과 같은 것을 씁니다(isCurrent 기준일).
      - 저장 뒤에는 엔진 저장소 캐시에서 그 고객사 항목을 버립니다(서비스 주석 참고).
"""


def get_operating_rule_service(
    db: AsyncSession = Depends(get_db),
    company_repo: CompanyRepository = Depends(get_company_repository),
    admin_assignment_repo: AdminAssignmentRepository = Depends(
        get_admin_assignment_repository
    ),
    clock: DecisionClock = Depends(get_decision_clock),
) -> OperatingRuleService:
    return OperatingRuleService(
        db=db,
        repo=OperatingRuleRepository(db),
        company_repo=company_repo,
        admin_assignment_repo=admin_assignment_repo,
        clock=clock,
        on_saved=engine_store_cache().evict_company,
    )
