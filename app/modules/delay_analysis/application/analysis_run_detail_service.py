import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rls_context import set_rls_context
from app.core.security.auth import CurrentUser
from app.modules.delay_analysis.application.delay_display import calculate_delay_days
from app.modules.delay_analysis.application.delay_event_query_service import (
    ShipmentDisplay,
    summarize_shipment,
)
from app.modules.delay_analysis.application.exception.analysis_run_query_exceptions import (
    AnalysisRunNotFoundException,
)
from app.modules.delay_analysis.infrastructure.repository.analysis_run_detail_repository import (
    AnalysisRunDetailRepository,
    AnalysisRunRow,
    AtRiskLineRow,
    DataVersionRow,
    FinalDecisionRow,
    InventoryImpactRow,
    MemberDraftRow,
    OptionOutcomeRow,
    ResponseOptionRow,
)
from app.modules.delay_analysis.infrastructure.repository.delay_event_read_repository import (
    DelayEventReadRepository,
    DelayEventRow,
)

"""
    분석 실행 상세 조회 서비스 (API 17, 명세 v6.2 4.5절)
      - 고객사 범위는 로그인 토큰의 company_id 로 정하고, 같은 값으로 RLS 컨텍스트를 설정합니다.
      - 없는 실행과 다른 고객사 실행은 같은 404(ANALYSIS_RUN_NOT_FOUND)입니다.
      - 저장된 결과를 그대로 돌려줍니다. 상태별로 비는 묶음(NO_DELAY·ON_HOLD·도구 실패 FAILED 의 결과 행 등)은
        저장기가 행을 만들지 않은 결과이고, 여기서 따로 지우거나 만들지 않습니다(명세 4.5 "상태별로 보이는 내용").
      - 대상은 지연 조회(4.4)와 같은 함수로 만듭니다(D13): 선적의 현재 단계·입고일, 경로, 대표 상품, 지연 일수.
      - dataWarnings 는 아직 빈 목록입니다. 입력 확정 때의 수집 상태를 남기는 일은 백그라운드 실행·저장 작업에서 합니다(GLOW-115 범위 밖).
"""


@dataclass(frozen=True)
class AnalysisTargetDetail:
    row: DelayEventRow
    shipment: ShipmentDisplay
    delay_days: int


@dataclass(frozen=True)
class AnalysisRunDetail:
    run: AnalysisRunRow
    target: AnalysisTargetDetail
    data_versions: list[DataVersionRow]
    data_basis_at: datetime | None
    inventory_impacts: list[InventoryImpactRow]
    at_risk_lines: list[AtRiskLineRow]
    response_options: list[ResponseOptionRow]
    option_outcomes: list[OptionOutcomeRow]
    member_drafts: list[MemberDraftRow]
    final_decision: FinalDecisionRow | None


class AnalysisRunDetailService:
    def __init__(
        self,
        db: AsyncSession,
        repo: AnalysisRunDetailRepository,
        delay_repo: DelayEventReadRepository,
    ):
        self.db = db
        self.repo = repo
        self.delay_repo = delay_repo

    async def get_analysis_run(
        self, *, actor: CurrentUser, run_id: uuid.UUID
    ) -> AnalysisRunDetail:
        company_id = await self._enter_company_scope(actor)
        run = await self.repo.find_run(company_id, run_id)
        if run is None:
            raise AnalysisRunNotFoundException()

        # 실행은 같은 고객사의 지연 이벤트를 참조합니다(복합 FK). 없을 수 없지만, 없으면 실행도 없는 것으로 봅니다.
        event = await self.delay_repo.find_delay_event(company_id, run.delay_event_id)
        if event is None:
            raise AnalysisRunNotFoundException()
        lines = await self.delay_repo.find_shipment_lines(
            company_id, [event.shipment_id]
        )
        target = AnalysisTargetDetail(
            row=event,
            shipment=summarize_shipment(event, lines),
            delay_days=calculate_delay_days(
                event.planned_3pl_inbound_date, event.expected_3pl_inbound_date
            ),
        )

        data_versions = await self.repo.find_data_versions(company_id, run_id)
        return AnalysisRunDetail(
            run=run,
            target=target,
            data_versions=data_versions,
            data_basis_at=oldest_finished_at(data_versions),
            inventory_impacts=await self.repo.find_inventory_impacts(
                company_id, run_id
            ),
            at_risk_lines=await self.repo.find_at_risk_lines(company_id, run_id),
            response_options=await self.repo.find_response_options(company_id, run_id),
            option_outcomes=await self.repo.find_option_outcomes(company_id, run_id),
            member_drafts=await self.repo.find_member_drafts(company_id, run_id),
            final_decision=await self.repo.find_final_decision(company_id, run_id),
        )

    async def _enter_company_scope(self, actor: CurrentUser) -> uuid.UUID:
        # 라우터가 Client 역할만 허용하므로 company_id 는 항상 있습니다.
        company_id = actor.company_id
        assert company_id is not None
        await set_rls_context(self.db, user_id=actor.user_id, company_id=company_id)
        return company_id


def oldest_finished_at(data_versions: list[DataVersionRow]) -> datetime | None:
    """dataBasisAt: 사용한 수집 중 가장 오래된 수집 시각(명세 4.5 입력 기준). 끝난 시각이 하나도 없으면 None"""
    finished = [v.finished_at for v in data_versions if v.finished_at is not None]
    return min(finished, default=None)
