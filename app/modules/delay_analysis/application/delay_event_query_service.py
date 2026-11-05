import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import PageParams
from app.core.rls_context import set_rls_context
from app.core.security.auth import CurrentUser
from app.modules.delay_analysis.application.delay_display import (
    ShipmentLineQuantity,
    build_route,
    calculate_delay_days,
    count_skus,
    pick_representative_line,
)
from app.modules.delay_analysis.application.exception.delay_event_exceptions import (
    DelayEventNotFoundException,
    InvalidDetectedPeriodException,
)
from app.modules.delay_analysis.application.stale.stale_service import (
    IN_PROGRESS_STATUSES,
)
from app.modules.delay_analysis.infrastructure.analysis_enums import AnalysisRunStatus
from app.modules.delay_analysis.infrastructure.repository.delay_event_read_repository import (
    DelayEventCounts,
    DelayEventFilter,
    DelayEventReadRepository,
    DelayEventRow,
    RunHistoryRow,
    ShipmentLineRow,
)

"""
    지연 목록·상세 조회 서비스 (API 15·16, API 명세 4.4절)
      - 고객사 범위는 로그인 토큰의 company_id 로 정하고, 같은 값으로 RLS 컨텍스트를 설정합니다.
      - 경로·대표 상품·SKU 수·지연 일수는 delay_display 의 순수 함수로 계산합니다 (D13).
      - 단계·계획·예상 입고일·지연 일수는 선적의 현재 값 기준입니다 (Repository 참고).
      - 표시용 실행 ID(runCode, D14)는 analysis_runs.run_code 마이그레이션 전이라 아직 없습니다.
"""

# 경로 출발지는 한국 출고 창고의 지역입니다. 고객사에 KR_ORIGIN 창고가 없으면 이 값을 씁니다.
DEFAULT_ORIGIN_REGION = "KR"


class DelayEventTab(StrEnum):
    """지연 목록 탭 = 현재 실행 상태 묶음 (API 명세 4.4절 탭 표). 값은 실행 상태 이름과 같게 둡니다."""

    ALL = "ALL"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    ANALYZING = "ANALYZING"
    ON_HOLD = "ON_HOLD"
    NEEDS_REANALYSIS = "NEEDS_REANALYSIS"


_TAB_STATUSES: dict[DelayEventTab, tuple[AnalysisRunStatus, ...] | None] = {
    DelayEventTab.ALL: None,
    DelayEventTab.READY_FOR_REVIEW: (AnalysisRunStatus.READY_FOR_REVIEW,),
    DelayEventTab.ANALYZING: IN_PROGRESS_STATUSES,
    DelayEventTab.ON_HOLD: (AnalysisRunStatus.ON_HOLD,),
    DelayEventTab.NEEDS_REANALYSIS: (AnalysisRunStatus.STALE, AnalysisRunStatus.FAILED),
}


@dataclass(frozen=True)
class ShipmentDisplay:
    """선적 상세 줄로 계산한 표시값 (D13). 상세 줄이 없으면 대표 상품은 None"""

    route: str
    representative_line: ShipmentLineRow | None
    sku_count: int


@dataclass(frozen=True)
class DelayEventListItem:
    row: DelayEventRow
    shipment: ShipmentDisplay
    delay_days: int


@dataclass(frozen=True)
class DelayEventList:
    items: list[DelayEventListItem]
    counts: DelayEventCounts
    total_count: int
    page: PageParams


@dataclass(frozen=True)
class DelayEventDetail:
    row: DelayEventRow
    shipment: ShipmentDisplay
    delay_days: int
    previous_expected_3pl_inbound_date: date | None
    runs: list[RunHistoryRow]


class DelayEventQueryService:
    def __init__(self, db: AsyncSession, repo: DelayEventReadRepository):
        self.db = db
        self.repo = repo

    async def list_delay_events(
        self,
        *,
        actor: CurrentUser,
        tab: DelayEventTab = DelayEventTab.ALL,
        warehouse_id: uuid.UUID | None = None,
        detected_from: datetime | None = None,
        detected_to: datetime | None = None,
        q: str | None = None,
        has_final_decision: bool | None = None,
        page: PageParams | None = None,
    ) -> DelayEventList:
        """
        감지 기간은 UTC 시각 [detected_from, detected_to) 입니다 (to 미포함, 명세 v6.2 C18).
        시간대가 있는 값만 받습니다(라우터가 거부). 다른 시간대 값은 UTC 로 맞춥니다.
        from > to 면 InvalidDetectedPeriodException(422), from = to 는 0건입니다 (명세 v6.2 C20).
        page 를 생략하면 첫 페이지(기본 크기)입니다. total_count 는 모든 필터를 적용한 전체 개수이고,
        counts(탭별 건수)는 탭·최종 선택 여부 필터를 뺀 값이라 서로 다를 수 있습니다.
        """
        if detected_from and detected_to and detected_from > detected_to:
            raise InvalidDetectedPeriodException()
        page = page or PageParams()
        company_id = await self._enter_company_scope(actor)
        filters = DelayEventFilter(
            warehouse_id=warehouse_id,
            detected_from=_to_utc(detected_from),
            detected_to=_to_utc(detected_to),
            q=q.strip() or None if q else None,
            run_statuses=_TAB_STATUSES[tab],
            has_final_decision=has_final_decision,
        )
        rows = await self.repo.find_delay_events(
            company_id, filters, limit=page.limit, offset=page.offset
        )
        total_count = await self.repo.count_delay_events(company_id, filters)
        counts = await self.repo.count_by_tab(company_id, filters)
        lines = await self.repo.find_shipment_lines(
            company_id, sorted({row.shipment_id for row in rows})
        )
        lines_by_shipment = _group_lines(lines)
        items = [
            DelayEventListItem(
                row=row,
                shipment=summarize_shipment(row, lines_by_shipment[row.shipment_id]),
                delay_days=calculate_delay_days(
                    row.planned_3pl_inbound_date, row.expected_3pl_inbound_date
                ),
            )
            for row in rows
        ]
        return DelayEventList(
            items=items, counts=counts, total_count=total_count, page=page
        )

    async def get_delay_event(
        self, *, actor: CurrentUser, delay_event_id: uuid.UUID
    ) -> DelayEventDetail:
        company_id = await self._enter_company_scope(actor)
        row = await self.repo.find_delay_event(company_id, delay_event_id)
        if row is None:
            raise DelayEventNotFoundException()
        lines = await self.repo.find_shipment_lines(company_id, [row.shipment_id])
        previous = (
            await self.repo.find_snapshot_expected_date(
                company_id, row.previous_shipment_schedule_snapshot_id
            )
            if row.previous_shipment_schedule_snapshot_id
            else None
        )
        return DelayEventDetail(
            row=row,
            shipment=summarize_shipment(row, lines),
            delay_days=calculate_delay_days(
                row.planned_3pl_inbound_date, row.expected_3pl_inbound_date
            ),
            previous_expected_3pl_inbound_date=previous,
            runs=await self.repo.find_runs(company_id, delay_event_id),
        )

    async def _enter_company_scope(self, actor: CurrentUser) -> uuid.UUID:
        # 라우터가 Client 역할만 허용하므로 company_id 는 항상 있습니다.
        company_id = actor.company_id
        assert company_id is not None
        await set_rls_context(self.db, user_id=actor.user_id, company_id=company_id)
        return company_id


def _to_utc(moment: datetime | None) -> datetime | None:
    return moment.astimezone(UTC) if moment else None


def _group_lines(
    lines: list[ShipmentLineRow],
) -> defaultdict[uuid.UUID, list[ShipmentLineRow]]:
    grouped: defaultdict[uuid.UUID, list[ShipmentLineRow]] = defaultdict(list)
    for line in lines:
        grouped[line.shipment_id].append(line)
    return grouped


def summarize_shipment(
    row: DelayEventRow, lines: list[ShipmentLineRow]
) -> ShipmentDisplay:
    """선적 표시값(D13). 지연 조회(4.4)와 분석 실행 상세(4.5) 대상이 같은 함수를 씁니다."""
    quantities = [
        ShipmentLineQuantity(
            line_no=line.line_no,
            sku_id=line.sku_id,
            shipped_quantity=line.shipped_quantity,
        )
        for line in lines
    ]
    representative = pick_representative_line(quantities)
    representative_line = (
        next(line for line in lines if line.line_no == representative.line_no)
        if representative
        else None
    )
    return ShipmentDisplay(
        route=build_route(
            row.origin_region_code or DEFAULT_ORIGIN_REGION, row.us_port_name
        ),
        representative_line=representative_line,
        sku_count=count_skus(quantities),
    )
