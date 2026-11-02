import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

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
)
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
      - 표시용 실행 ID(runCode, D14)는 analysis_runs.run_code 마이그레이션 전이라 아직 없습니다.
"""

# 경로 출발지는 한국 출고 창고의 지역입니다. 고객사에 KR_ORIGIN 창고가 없으면 이 값을 씁니다.
DEFAULT_ORIGIN_REGION = "KR"


class DelayEventTab(StrEnum):
    """지연 목록 탭 = 현재 실행 상태 묶음 (API 명세 4.4절 탭 표)"""

    ALL = "ALL"
    PENDING_DECISION = "PENDING_DECISION"
    ANALYZING = "ANALYZING"
    ON_HOLD = "ON_HOLD"
    NEEDS_REANALYSIS = "NEEDS_REANALYSIS"


_TAB_STATUSES: dict[DelayEventTab, tuple[str, ...] | None] = {
    DelayEventTab.ALL: None,
    DelayEventTab.PENDING_DECISION: ("READY_FOR_REVIEW",),
    DelayEventTab.ANALYZING: (
        "DETECTED",
        "VALIDATING",
        "ANALYZING_IMPACT",
        "ANALYZING_OPTIONS",
        "VALIDATING_OUTPUT",
    ),
    DelayEventTab.ON_HOLD: ("ON_HOLD",),
    DelayEventTab.NEEDS_REANALYSIS: ("STALE", "FAILED"),
}


@dataclass(frozen=True)
class ShipmentSummary:
    route: str
    representative_sku: str | None
    representative_sku_code: str | None
    sku_count: int


@dataclass(frozen=True)
class DelayEventListItem:
    row: DelayEventRow
    shipment: ShipmentSummary
    delay_days: int


@dataclass(frozen=True)
class DelayEventList:
    items: list[DelayEventListItem]
    counts: DelayEventCounts


@dataclass(frozen=True)
class DelayEventDetail:
    row: DelayEventRow
    shipment: ShipmentSummary
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
        detected_from: date | None = None,
        detected_to: date | None = None,
        q: str | None = None,
        has_final_decision: bool | None = None,
    ) -> DelayEventList:
        """감지 기간은 UTC 날짜 기준이며 detected_to 날짜를 포함합니다."""
        company_id = await self._enter_company_scope(actor)
        filters = DelayEventFilter(
            warehouse_id=warehouse_id,
            detected_from=_start_of_day(detected_from),
            detected_to=_start_of_day(detected_to + timedelta(days=1))
            if detected_to
            else None,
            q=q.strip() or None if q else None,
            run_statuses=_TAB_STATUSES[tab],
            has_final_decision=has_final_decision,
        )
        rows = await self.repo.find_delay_events(company_id, filters)
        counts = await self.repo.count_by_tab(company_id, filters)
        lines = await self.repo.find_shipment_lines(
            company_id, sorted({row.shipment_id for row in rows})
        )
        lines_by_shipment = _group_lines(lines)
        items = [
            DelayEventListItem(
                row=row,
                shipment=_summarize(row, lines_by_shipment[row.shipment_id]),
                delay_days=calculate_delay_days(
                    row.planned_3pl_inbound_date, row.expected_3pl_inbound_date
                ),
            )
            for row in rows
        ]
        return DelayEventList(items=items, counts=counts)

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
            shipment=_summarize(row, lines),
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


def _start_of_day(day: date | None) -> datetime | None:
    return datetime.combine(day, time.min, tzinfo=UTC) if day else None


def _group_lines(
    lines: list[ShipmentLineRow],
) -> defaultdict[uuid.UUID, list[ShipmentLineRow]]:
    grouped: defaultdict[uuid.UUID, list[ShipmentLineRow]] = defaultdict(list)
    for line in lines:
        grouped[line.shipment_id].append(line)
    return grouped


def _summarize(row: DelayEventRow, lines: list[ShipmentLineRow]) -> ShipmentSummary:
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
    return ShipmentSummary(
        route=build_route(
            row.origin_region_code or DEFAULT_ORIGIN_REGION, row.us_port_name
        ),
        representative_sku=representative_line.product_name
        if representative_line
        else None,
        representative_sku_code=representative_line.sku_code
        if representative_line
        else None,
        sku_count=count_skus(quantities),
    )
