import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

import pytest

from app.core.pagination import PageParams
from app.core.security.auth import CurrentUser
from app.modules.delay_analysis.application import delay_event_query_service
from app.modules.delay_analysis.application.delay_event_query_service import (
    DelayEventQueryService,
    DelayEventTab,
)
from app.modules.delay_analysis.application.exception.delay_event_exceptions import (
    DelayEventNotFoundException,
)
from app.modules.delay_analysis.infrastructure.repository.delay_event_read_repository import (
    DelayEventCounts,
    DelayEventRow,
    RunHistoryRow,
    ShipmentLineRow,
)

COMPANY_ID = uuid.uuid7()
ACTOR = CurrentUser(
    user_id=uuid.uuid7(), role_code="CLIENT_LEAD", company_id=COMPANY_ID
)
SHIPMENT_ID = uuid.uuid7()
SNAPSHOT_ID = uuid.uuid7()
EVENT_ID = uuid.uuid7()
COUNTS = DelayEventCounts(
    all_count=1, ready_for_review=1, analyzing=0, on_hold=0, needs_reanalysis=0
)


def _row(**kwargs) -> DelayEventRow:
    values = {
        "delay_event_id": EVENT_ID,
        "shipment_id": SHIPMENT_ID,
        "stage": "AT_SEA",
        "planned_3pl_inbound_date": date(2026, 1, 27),
        "expected_3pl_inbound_date": date(2026, 1, 30),
        "detected_at": datetime(2026, 1, 20, tzinfo=UTC),
        "closed_at": None,
        "close_reason": None,
        "previous_shipment_schedule_snapshot_id": SNAPSHOT_ID,
        "shipment_code": "OCN-20251215",
        "transport_mode": "OCEAN",
        "warehouse_id": uuid.uuid7(),
        "warehouse_code": "WH-WEST",
        "region_code": "WEST",
        "us_port_name": "LOS ANGELES, CA",
        "origin_region_code": "KR",
        "run_id": uuid.uuid7(),
        "run_status": "READY_FOR_REVIEW",
        "has_final_decision": False,
        "at_risk_order_count": 4,
    } | kwargs
    return DelayEventRow(**values)


def _line(line_no: int, quantity: int, name: str) -> ShipmentLineRow:
    return ShipmentLineRow(
        shipment_id=SHIPMENT_ID,
        line_no=line_no,
        sku_id=uuid.uuid7(),
        shipped_quantity=quantity,
        sku_code=f"SKU-{line_no:03d}",
        product_name=name,
    )


@dataclass
class FakeRepository:
    rows: list = field(default_factory=list)
    lines: list = field(default_factory=list)
    runs: list = field(default_factory=list)
    filters: list = field(default_factory=list)
    pages: list = field(default_factory=list)
    total_count: int | None = None

    async def find_delay_events(self, company_id, filters, *, limit=None, offset=0):
        self.filters.append(filters)
        self.pages.append((limit, offset))
        return self.rows

    async def count_delay_events(self, company_id, filters):
        return len(self.rows) if self.total_count is None else self.total_count

    async def count_by_tab(self, company_id, filters):
        return COUNTS

    async def find_delay_event(self, company_id, delay_event_id):
        return next((r for r in self.rows if r.delay_event_id == delay_event_id), None)

    async def find_shipment_lines(self, company_id, shipment_ids):
        return [line for line in self.lines if line.shipment_id in shipment_ids]

    async def find_snapshot_expected_date(self, company_id, snapshot_id):
        return date(2026, 1, 28)

    async def find_runs(self, company_id, delay_event_id):
        return self.runs


@pytest.fixture(autouse=True)
def rls_calls(monkeypatch):
    calls = []

    async def fake_set_rls_context(db, *, user_id, company_id):
        calls.append(company_id)

    monkeypatch.setattr(
        delay_event_query_service, "set_rls_context", fake_set_rls_context
    )
    return calls


def _service(repo) -> DelayEventQueryService:
    return DelayEventQueryService(db=object(), repo=repo)


# --- 목록 ------------------------------------------------------------------------


async def test_list_computes_route_representative_sku_and_delay_days(rls_calls):
    repo = FakeRepository(
        rows=[_row()],
        lines=[_line(1, 100, "토너"), _line(2, 300, "세럼"), _line(3, 300, "크림")],
    )

    result = await _service(repo).list_delay_events(actor=ACTOR)

    [item] = result.items
    assert item.shipment.route == "KR → LOS ANGELES"
    # 300개 동률이면 줄 번호가 작은 쪽
    assert item.shipment.representative_line.product_name == "세럼"
    assert item.shipment.representative_line.sku_code == "SKU-002"
    assert item.shipment.sku_count == 3
    assert item.delay_days == 3
    assert result.counts == COUNTS
    assert rls_calls == [COMPANY_ID]


async def test_list_maps_tab_and_date_range_to_filters():
    repo = FakeRepository()

    await _service(repo).list_delay_events(
        actor=ACTOR,
        tab=DelayEventTab.NEEDS_REANALYSIS,
        detected_from=date(2026, 1, 1),
        detected_to=date(2026, 1, 31),
        q="  ",
    )

    [filters] = repo.filters
    assert filters.run_statuses == ("STALE", "FAILED")
    assert filters.detected_from == datetime(2026, 1, 1, tzinfo=UTC)
    assert filters.detected_to == datetime(2026, 2, 1, tzinfo=UTC)  # to 날짜 포함
    assert filters.q is None


async def test_list_all_tab_has_no_status_filter():
    repo = FakeRepository()

    await _service(repo).list_delay_events(actor=ACTOR)

    assert repo.filters[0].run_statuses is None


async def test_list_defaults_to_first_page():
    repo = FakeRepository(rows=[_row()])

    result = await _service(repo).list_delay_events(actor=ACTOR)

    assert repo.pages == [(20, 0)]
    assert (result.page.page, result.page.size, result.total_count) == (1, 20, 1)


async def test_list_passes_page_to_repository_and_returns_filtered_total():
    repo = FakeRepository(rows=[_row()], total_count=45)

    result = await _service(repo).list_delay_events(
        actor=ACTOR, page=PageParams(page=3, size=10)
    )

    assert repo.pages == [(10, 20)]
    assert result.total_count == 45
    assert result.counts == COUNTS  # 탭별 건수는 total_count 와 따로 옵니다


async def test_list_uses_default_origin_and_handles_shipment_without_lines():
    repo = FakeRepository(rows=[_row(origin_region_code=None)])

    [item] = (await _service(repo).list_delay_events(actor=ACTOR)).items

    assert item.shipment.route == "KR → LOS ANGELES"
    assert item.shipment.representative_line is None
    assert item.shipment.sku_count == 0


# --- 상세 ------------------------------------------------------------------------


async def test_detail_returns_previous_expected_date_and_runs():
    run = RunHistoryRow(
        analysis_run_id=uuid.uuid7(),
        version_no=1,
        status="READY_FOR_REVIEW",
        is_latest=True,
        trigger_type="AUTO",
        parent_analysis_run_id=None,
        stale_detected_at=None,
        stale_primary_reason=None,
        started_at=datetime(2026, 1, 20, tzinfo=UTC),
        finished_at=None,
    )
    repo = FakeRepository(rows=[_row()], lines=[_line(1, 10, "토너")], runs=[run])

    detail = await _service(repo).get_delay_event(actor=ACTOR, delay_event_id=EVENT_ID)

    assert detail.previous_expected_3pl_inbound_date == date(2026, 1, 28)
    assert detail.runs == [run]
    assert detail.shipment.representative_line.product_name == "토너"


async def test_detail_raises_not_found_for_unknown_event():
    with pytest.raises(DelayEventNotFoundException):
        await _service(FakeRepository()).get_delay_event(
            actor=ACTOR, delay_event_id=uuid.uuid7()
        )
