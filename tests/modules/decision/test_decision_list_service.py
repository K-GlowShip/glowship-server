import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.core.pagination import PageParams
from app.core.security.tenant import TenantContext
from app.modules.decision.application.decision_list_service import (
    DecisionListService,
)
from app.modules.decision.application.exception.decision_exceptions import (
    InvalidDecidedPeriodException,
)
from app.modules.decision.infrastructure.repository.decision_repository import (
    DecisionFilter,
    DecisionPage,
    DecisionRow,
    ShipmentLineRow,
)

TENANT = TenantContext(user_id=uuid.uuid7(), company_id=uuid.uuid7())
SHIPMENT_ID = uuid.uuid7()
OTHER_SHIPMENT_ID = uuid.uuid7()
SKU_A, SKU_B = uuid.uuid7(), uuid.uuid7()
KST = timezone(timedelta(hours=9))


@dataclass
class FakeDecisionRepository:
    page: DecisionPage = field(
        default_factory=lambda: DecisionPage(rows=[], total_count=0)
    )
    lines: list[ShipmentLineRow] = field(default_factory=list)
    calls: list = field(default_factory=list)

    async def find_page(self, company_id, filters, **kwargs):
        self.calls.append(("find_page", company_id, filters, kwargs))
        return self.page

    async def find_shipment_lines(self, company_id, shipment_ids):
        self.calls.append(("find_shipment_lines", company_id, list(shipment_ids)))
        return [line for line in self.lines if line.shipment_id in shipment_ids]


def _row(**overrides) -> DecisionRow:
    values = {
        "decision_type": "DRAFT",
        "decision_id": uuid.uuid7(),
        "decided_at": datetime(2026, 1, 1, 2, tzinfo=UTC),
        "status": "DRAFT",
        "run_id": uuid.uuid7(),
        "run_code": "AN-260101-003",
        "run_status": "READY_FOR_REVIEW",
        "run_result_class": "FORECAST_ONLY_READY",
        "run_failure_code": None,
        "delay_event_id": uuid.uuid7(),
        "shipment_id": SHIPMENT_ID,
        "shipment_code": "OCN-20251215",
        "user_id": uuid.uuid7(),
        "display_name": "A Beauty Company Member 1",
        "role_code": "CLIENT_MEMBER",
        "response_option_id": uuid.uuid7(),
        "option_type": "KR_AIR_REPLENISH",
    }
    values.update(overrides)
    return DecisionRow(**values)


def _line(
    shipment_id, line_no, sku_id, quantity, code="SKU-001", name="토너"
) -> ShipmentLineRow:
    return ShipmentLineRow(
        shipment_id=shipment_id,
        line_no=line_no,
        sku_id=sku_id,
        shipped_quantity=quantity,
        sku_code=code,
        product_name=name,
    )


def _service(rows=(), total_count=None, lines=()):
    repo = FakeDecisionRepository(
        page=DecisionPage(rows=list(rows), total_count=total_count or len(rows)),
        lines=list(lines),
    )
    return DecisionListService(repo=repo), repo


# --- 필터·페이지 -----------------------------------------------------------------


async def test_필터와_페이지를_리포지토리에_그대로_넘기고_결과를_감싼다():
    service, repo = _service(rows=[_row()], total_count=7)
    decided_by, run_id = uuid.uuid7(), uuid.uuid7()

    result = await service.get_page(
        TENANT,
        decided_from=datetime(2026, 1, 1, tzinfo=UTC),
        decided_to=datetime(2026, 2, 1, tzinfo=UTC),
        decision_type="DRAFT",
        status="NEEDS_REANALYSIS",
        decided_by=decided_by,
        run_id=run_id,
        query="  OCN ",
        page=PageParams(page=2, size=5),
    )

    _, company_id, filters, kwargs = repo.calls[0]
    assert company_id == TENANT.company_id
    assert filters == DecisionFilter(
        decided_from=datetime(2026, 1, 1, tzinfo=UTC),
        decided_to=datetime(2026, 2, 1, tzinfo=UTC),
        decision_type="DRAFT",
        status="NEEDS_REANALYSIS",
        decided_by=decided_by,
        run_id=run_id,
        query="OCN",  # 양끝 공백 제거
    )
    assert kwargs == {"offset": 5, "limit": 5}
    assert (result.total_count, result.page.page, result.page.size) == (7, 2, 5)
    assert len(result.items) == 1


async def test_생략한_값은_None_이고_기본_페이지는_1쪽_20건이다():
    service, repo = _service()

    result = await service.get_page(TENANT, query="   ")

    _, _, filters, kwargs = repo.calls[0]
    assert filters == DecisionFilter()  # 공백뿐인 검색어는 없는 것
    assert kwargs == {"offset": 0, "limit": 20}
    assert result.items == [] and result.total_count == 0


async def test_기간은_UTC_로_바꾸어_넘긴다():
    service, repo = _service()

    await service.get_page(
        TENANT,
        decided_from=datetime(2026, 1, 1, 9, tzinfo=KST),
        decided_to=datetime(2026, 1, 2, 9, tzinfo=KST),
    )

    filters = repo.calls[0][2]
    assert filters.decided_from == datetime(2026, 1, 1, 0, tzinfo=UTC)
    assert filters.decided_to == datetime(2026, 1, 2, 0, tzinfo=UTC)


async def test_from_이_to_보다_늦으면_422_이고_DB_를_조회하지_않는다():
    service, repo = _service()

    with pytest.raises(InvalidDecidedPeriodException):
        await service.get_page(
            TENANT,
            decided_from=datetime(2026, 1, 3, tzinfo=UTC),
            decided_to=datetime(2026, 1, 2, tzinfo=UTC),
        )

    assert repo.calls == []


async def test_from_과_to_가_같으면_오류가_아니다():
    service, repo = _service()
    same = datetime(2026, 1, 2, tzinfo=UTC)

    result = await service.get_page(TENANT, decided_from=same, decided_to=same)

    assert result.items == []
    assert repo.calls[0][2].decided_from == repo.calls[0][2].decided_to == same


# --- 행 조립 ---------------------------------------------------------------------


async def test_행의_값을_그대로_옮기고_대표_상품은_수량이_가장_많은_SKU_다():
    row = _row(status="NEEDS_REANALYSIS", run_status="STALE")
    service, repo = _service(
        rows=[row],
        lines=[
            _line(SHIPMENT_ID, 1, SKU_A, 10, "SKU-001", "토너"),
            _line(SHIPMENT_ID, 2, SKU_B, 30, "SKU-002", "세럼"),
        ],
    )

    [item] = (await service.get_page(TENANT)).items

    assert (item.decision_type, item.decision_id, item.status) == (
        "DRAFT",
        row.decision_id,
        "NEEDS_REANALYSIS",
    )
    assert (item.run_id, item.run_code, item.run_status) == (
        row.run_id,
        "AN-260101-003",
        "STALE",
    )
    assert (item.user_id, item.display_name, item.role_code) == (
        row.user_id,
        "A Beauty Company Member 1",
        "CLIENT_MEMBER",
    )
    assert (item.response_option_id, item.option_type) == (
        row.response_option_id,
        "KR_AIR_REPLENISH",
    )
    assert (
        item.representative_sku_id,
        item.representative_sku_code,
        item.representative_product_name,
    ) == (SKU_B, "SKU-002", "세럼")
    # 페이지의 선적 ID 로 상세 줄을 한 번에 읽는다
    assert repo.calls[1] == ("find_shipment_lines", TENANT.company_id, [SHIPMENT_ID])


async def test_수량이_같으면_줄_번호가_작은_SKU_가_대표다():
    service, _ = _service(
        rows=[_row()],
        lines=[
            _line(SHIPMENT_ID, 2, SKU_B, 10, "SKU-002", "세럼"),
            _line(SHIPMENT_ID, 1, SKU_A, 10, "SKU-001", "토너"),
        ],
    )

    [item] = (await service.get_page(TENANT)).items

    assert item.representative_sku_code == "SKU-001"


async def test_상세_줄이_없으면_대표_상품이_None_이다():
    service, _ = _service(rows=[_row()], lines=[])

    [item] = (await service.get_page(TENANT)).items

    assert item.representative_sku_id is None
    assert item.representative_sku_code is None
    assert item.representative_product_name is None


async def test_의견만_쓴_초안은_대응안이_None_이다():
    service, _ = _service(rows=[_row(response_option_id=None, option_type=None)])

    [item] = (await service.get_page(TENANT)).items

    assert item.response_option_id is None and item.option_type is None


async def test_같은_선적의_여러_행은_각자_대표_상품을_받는다():
    final = _row(decision_type="FINAL", status="FINAL", role_code="CLIENT_LEAD")
    draft_other = _row(shipment_id=OTHER_SHIPMENT_ID, shipment_code="AIR-REG-20251229")
    service, repo = _service(
        rows=[final, _row(), draft_other],
        lines=[
            _line(SHIPMENT_ID, 1, SKU_B, 5, "SKU-002", "세럼"),
            _line(OTHER_SHIPMENT_ID, 1, SKU_A, 5, "SKU-001", "토너"),
        ],
    )

    items = (await service.get_page(TENANT)).items

    assert [i.representative_sku_code for i in items] == [
        "SKU-002",
        "SKU-002",
        "SKU-001",
    ]
    assert repo.calls[1][2] == [SHIPMENT_ID, SHIPMENT_ID, OTHER_SHIPMENT_ID]


async def test_행이_없으면_상세_줄을_빈_목록으로_조회한다():
    service, repo = _service()

    await service.get_page(TENANT)

    assert repo.calls[1] == ("find_shipment_lines", TENANT.company_id, [])
