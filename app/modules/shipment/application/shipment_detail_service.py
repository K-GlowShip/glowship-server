import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from app.core.security.tenant import TenantContext
from app.modules.shipment.application.exception.shipment_exceptions import (
    ShipmentNotFoundException,
)
from app.modules.shipment.application.shipment_list_service import (
    ShipmentListItem,
    ShipmentListService,
)
from app.modules.shipment.infrastructure.repository.shipment_repository import (
    ShipmentLineRow,
    ShipmentRepository,
    ShipmentScheduleRow,
)

"""
    선적 상세 서비스 (API 명세 v6.2 4.3 GET /shipments/{shipmentId}, 연동 데이터 조회 화면 SCR-005 의 선적 상세)
      - 응답은 목록 필드 + 현재 예상 항구 도착일 + 상세 줄(lines) + 계획 근거(plannedBasis) + 일정 이력(scheduleHistory)
        + 데이터 기준 시각(dataBasisAt)이다.
      - 목록 필드는 목록 서비스의 같은 계산(_build_item: 경로·대표 상품·품목 수·지연 일수, D13)을 그대로 써서
        목록과 상세의 같은 칸이 어긋나지 않는다.
      - 고객사 범위는 토큰의 company_id(TenantContext)다. 없거나 다른 고객사 선적이면 404 SHIPMENT_NOT_FOUND.
      - 항구 도착일과 3PL 입고일은 다른 칸이다(BR-007). 항구 도착일은 선적의 현재 값(current_expected_port_arrival_date)이다.
      - 일정 이력은 관측 시각 오래된 순 전부다. 가상 시각으로 거르지 않는다(목록도 거르지 않음, 시연 DB 는 가상 시각까지만 적재).
      - SQL 은 3회(선적 한 행·상세 줄·일정 이력)다.
"""


@dataclass(frozen=True)
class ShipmentDetailLine:
    line_no: int
    sku_id: uuid.UUID
    sku_code: str
    product_name: str
    shipped_quantity: int


@dataclass(frozen=True)
class ShipmentPlannedBasis:
    """계획 3PL 입고일을 정한 시장 리드타임 기준 (market_leadtime_references)"""

    month: str
    us_port_name: str
    total_leadtime_p50: Decimal
    total_leadtime_p90: Decimal
    confidence_leadtime: str


@dataclass(frozen=True)
class ShipmentScheduleObservation:
    observed_at: datetime
    stage: str
    expected_port_arrival_date: date | None
    expected_3pl_inbound_date: date


@dataclass(frozen=True)
class ShipmentDetail:
    item: ShipmentListItem
    expected_port_arrival_date: date | None
    lines: list[ShipmentDetailLine]
    planned_basis: ShipmentPlannedBasis
    schedule_history: list[ShipmentScheduleObservation]
    data_basis_at: datetime | None


class ShipmentDetailService:
    """선적 상세 한 건을 만든다."""

    def __init__(self, repo: ShipmentRepository):
        self.repo = repo

    async def get(
        self, tenant: TenantContext, shipment_id: uuid.UUID
    ) -> ShipmentDetail:
        row = await self.repo.find_detail(tenant.company_id, shipment_id)
        if row is None:
            raise ShipmentNotFoundException()
        lines = await self.repo.find_lines(tenant.company_id, [shipment_id])
        history = await self.repo.find_history(tenant.company_id, shipment_id)
        return ShipmentDetail(
            item=ShipmentListService._build_item(row, lines),
            expected_port_arrival_date=row.expected_port_arrival_date,
            lines=[_line(line) for line in lines],
            planned_basis=ShipmentPlannedBasis(
                month=row.month,
                us_port_name=row.us_port_name,
                total_leadtime_p50=row.total_leadtime_p50,
                total_leadtime_p90=row.total_leadtime_p90,
                confidence_leadtime=row.confidence_leadtime,
            ),
            schedule_history=[_observation(snapshot) for snapshot in history],
            data_basis_at=row.data_basis_at,
        )


def _line(line: ShipmentLineRow) -> ShipmentDetailLine:
    return ShipmentDetailLine(
        line_no=line.line_no,
        sku_id=line.sku_id,
        sku_code=line.sku_code,
        product_name=line.product_name,
        shipped_quantity=line.shipped_quantity,
    )


def _observation(snapshot: ShipmentScheduleRow) -> ShipmentScheduleObservation:
    return ShipmentScheduleObservation(
        observed_at=snapshot.observed_at,
        stage=snapshot.stage,
        expected_port_arrival_date=snapshot.expected_port_arrival_date,
        expected_3pl_inbound_date=snapshot.expected_3pl_inbound_date,
    )
