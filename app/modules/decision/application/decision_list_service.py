import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.pagination import PageParams
from app.core.security.tenant import TenantContext
from app.modules.decision.application.exception.decision_exceptions import (
    InvalidDecidedPeriodException,
)
from app.modules.decision.infrastructure.repository.decision_repository import (
    DecisionFilter,
    DecisionRepository,
    DecisionRow,
    ShipmentLineRow,
)
from app.modules.delay_analysis.application.delay_display import (
    ShipmentLineQuantity,
    pick_representative_line,
)

"""
    판단 이력 서비스 (API 명세 v6.2 4.7 GET /decisions, 의사결정 이력 화면 SCR-004 의 목록)
      - 고객사 범위는 토큰의 company_id(TenantContext)다. RLS 컨텍스트는 get_tenant_context 가 설정해 둔다.
      - 기간 from·to 는 UTC 로 맞추고, from > to 면 422 (명세 C20). from = to 는 0건이다.
      - 대표 상품(representativeSku)은 명세 4.7 대로 지연 목록과 같은 계산(D13)이다:
        선적에서 수량이 가장 많은 SKU, 같으면 상세 줄 번호가 작은 SKU, 상세가 없으면 None.
        delay_analysis 모듈의 delay_display 함수를 그대로 쓴다.
      - 선택 이유·의견 본문, 당시 분석 수치는 목록에 넣지 않는다. 화면 아래 패널은 GET /analysis-runs/{runId} 몫이다.
"""


@dataclass(frozen=True)
class DecisionListItem:
    """판단 이력 한 행. 응답 스키마와 같은 필드 이름이다. 대표 상품·대응안은 없으면 None 이다."""

    decision_type: str
    decision_id: uuid.UUID
    decided_at: datetime
    status: str
    shipment_id: uuid.UUID
    shipment_code: str
    representative_sku_id: uuid.UUID | None
    representative_sku_code: str | None
    representative_product_name: str | None
    response_option_id: uuid.UUID | None
    option_type: str | None
    user_id: uuid.UUID
    display_name: str
    role_code: str
    run_id: uuid.UUID
    run_code: str | None
    run_status: str
    run_result_class: str | None
    run_failure_code: str | None
    delay_event_id: uuid.UUID


@dataclass(frozen=True)
class DecisionListResult:
    items: list[DecisionListItem]
    total_count: int
    page: PageParams


class DecisionListService:
    """의사결정 이력 화면의 목록 한 페이지를 만든다."""

    def __init__(self, repo: DecisionRepository):
        self.repo = repo

    async def get_page(
        self,
        tenant: TenantContext,
        *,
        decided_from: datetime | None = None,
        decided_to: datetime | None = None,
        decision_type: str | None = None,
        status: str | None = None,
        decided_by: uuid.UUID | None = None,
        run_id: uuid.UUID | None = None,
        query: str | None = None,
        page: PageParams | None = None,
    ) -> DecisionListResult:
        """page 를 생략하면 첫 페이지(기본 크기)다. 검색어는 양끝 공백을 지우고 비면 없는 것으로 본다.
        from 이 to 보다 늦으면 InvalidDecidedPeriodException(422)."""
        decided_from, decided_to = _to_utc(decided_from), _to_utc(decided_to)
        if decided_from and decided_to and decided_from > decided_to:
            raise InvalidDecidedPeriodException()
        page = page or PageParams()
        filters = DecisionFilter(
            decided_from=decided_from,
            decided_to=decided_to,
            decision_type=decision_type,
            status=status,
            decided_by=decided_by,
            run_id=run_id,
            query=query.strip() or None if query else None,
        )
        repo_page = await self.repo.find_page(
            tenant.company_id, filters, offset=page.offset, limit=page.limit
        )
        lines = _group_lines(
            await self.repo.find_shipment_lines(
                tenant.company_id, [row.shipment_id for row in repo_page.rows]
            )
        )
        return DecisionListResult(
            items=[
                self._build_item(row, lines[row.shipment_id]) for row in repo_page.rows
            ],
            total_count=repo_page.total_count,
            page=page,
        )

    @staticmethod
    def _build_item(row: DecisionRow, lines: list[ShipmentLineRow]) -> DecisionListItem:
        representative = _representative(lines)
        return DecisionListItem(
            decision_type=row.decision_type,
            decision_id=row.decision_id,
            decided_at=row.decided_at,
            status=row.status,
            shipment_id=row.shipment_id,
            shipment_code=row.shipment_code,
            representative_sku_id=representative.sku_id if representative else None,
            representative_sku_code=(
                representative.sku_code if representative else None
            ),
            representative_product_name=(
                representative.product_name if representative else None
            ),
            response_option_id=row.response_option_id,
            option_type=row.option_type,
            user_id=row.user_id,
            display_name=row.display_name,
            role_code=row.role_code,
            run_id=row.run_id,
            run_code=row.run_code,
            run_status=row.run_status,
            run_result_class=row.run_result_class,
            run_failure_code=row.run_failure_code,
            delay_event_id=row.delay_event_id,
        )


def _to_utc(moment: datetime | None) -> datetime | None:
    """시간대가 있는 시각을 UTC 로 바꾼다. 응답·비교가 모두 UTC 기준이다."""
    return moment.astimezone(UTC) if moment is not None else None


def _representative(lines: list[ShipmentLineRow]) -> ShipmentLineRow | None:
    """수량이 가장 많은 상세 줄(같으면 줄 번호가 작은 줄). 상세가 없으면 None (D13, 지연 목록과 같은 규칙)."""
    picked = pick_representative_line(
        ShipmentLineQuantity(
            line_no=line.line_no,
            sku_id=line.sku_id,
            shipped_quantity=line.shipped_quantity,
        )
        for line in lines
    )
    if picked is None:
        return None
    return next(line for line in lines if line.line_no == picked.line_no)


def _group_lines(
    lines: list[ShipmentLineRow],
) -> defaultdict[uuid.UUID, list[ShipmentLineRow]]:
    grouped: defaultdict[uuid.UUID, list[ShipmentLineRow]] = defaultdict(list)
    for line in lines:
        grouped[line.shipment_id].append(line)
    return grouped
