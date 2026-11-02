import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from app.core.security.tenant import TenantContext
from app.modules.data_sync.application.data_sync_status_service import (
    DataSyncStatusService,
)
from app.modules.data_sync.infrastructure.data_sync_enums import SourceDomain
from app.modules.data_sync.infrastructure.repository.data_sync_run_repository import (
    LatestRunSummary,
)

TENANT = TenantContext(user_id=uuid.uuid7(), company_id=uuid.uuid7())
NOW = datetime(2026, 11, 2, 11, 0, tzinfo=UTC)
INTERVAL = timedelta(minutes=10)
ALL_DOMAINS = ["ORDER", "INVENTORY", "SHIPMENT", "PRODUCT", "WAREHOUSE"]


@dataclass
class FakeDataSyncRunRepository:
    summaries: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)

    async def find_latest_by_domain(self, company_id, source_domains):
        self.calls.append((company_id, list(source_domains)))
        return self.summaries


def _summary(
    *,
    started_ago: timedelta = timedelta(minutes=5),
    completed_status: str | None = "SUCCEEDED",
    synced_ago: timedelta | None = timedelta(minutes=5),
) -> LatestRunSummary:
    return LatestRunSummary(
        latest_started_at=NOW - started_ago,
        latest_completed_status=completed_status,
        last_synced_at=NOW - synced_ago if synced_ago is not None else None,
    )


def _service(summaries: dict, stale_multiplier: int = 2):
    repo = FakeDataSyncRunRepository(summaries=summaries)
    service = DataSyncStatusService(
        repo=repo, stale_multiplier=stale_multiplier, clock=lambda: NOW
    )
    return service, repo


async def _order_status(summaries: dict, stale_multiplier: int = 2):
    service, _ = _service(summaries, stale_multiplier)
    statuses = await service.get_latest_statuses(TENANT)
    return statuses[0]


# --- 항목 구성 -----------------------------------------------------------------


async def test_카탈로그_순서대로_다섯_항목을_돌려주고_고객사와_원천으로_조회한다():
    service, repo = _service({})

    statuses = await service.get_latest_statuses(TENANT)

    assert [s.source_domain for s in statuses] == [
        SourceDomain.ORDER,
        SourceDomain.INVENTORY,
        SourceDomain.SHIPMENT,
        SourceDomain.PRODUCT,
        SourceDomain.WAREHOUSE,
    ]
    assert [s.source_system_name for s in statuses] == [
        "SAP ERP",
        "WMS",
        "TMS",
        "SAP ERP",
        "WMS",
    ]
    assert all(s.sync_interval_minutes == 10 for s in statuses)
    assert repo.calls == [(TENANT.company_id, ALL_DOMAINS)]


async def test_수집_이력이_없으면_동기화_안_됨이고_시각은_null이다():
    status = await _order_status({})

    assert status.is_synced is False
    assert status.last_synced_at is None


# --- isSynced 판정 ---------------------------------------------------------------


async def test_최근_수집이_성공이고_주기_안이면_동기화됨이다():
    status = await _order_status({"ORDER": _summary()})

    assert status.is_synced is True
    assert status.last_synced_at == NOW - timedelta(minutes=5)


async def test_부분_성공도_동기화됨으로_본다():
    status = await _order_status(
        {"ORDER": _summary(completed_status="PARTIAL_SUCCESS")}
    )

    assert status.is_synced is True


async def test_최근_수집이_실패면_동기화_안_됨이고_이전_성공_시각을_유지한다():
    status = await _order_status(
        {
            "ORDER": _summary(
                started_ago=timedelta(minutes=5),
                completed_status="FAILED",
                synced_ago=timedelta(minutes=15),
            )
        }
    )

    assert status.is_synced is False
    assert status.last_synced_at == NOW - timedelta(minutes=15)


async def test_진행_중이면_직전에_끝난_실행으로_판정한다():
    # 리포지토리가 RUNNING 을 뺀 최근 실행 상태를 completed 로 주므로, 서비스는 그 값만 본다.
    running_after_success = _summary(
        started_ago=timedelta(minutes=1), completed_status="SUCCEEDED"
    )
    running_after_failure = _summary(
        started_ago=timedelta(minutes=1), completed_status="FAILED"
    )

    assert (await _order_status({"ORDER": running_after_success})).is_synced is True
    assert (await _order_status({"ORDER": running_after_failure})).is_synced is False


async def test_끝난_실행이_아직_없으면_동기화_안_됨이다():
    first_run_in_progress = _summary(
        started_ago=timedelta(minutes=1), completed_status=None, synced_ago=None
    )

    status = await _order_status({"ORDER": first_run_in_progress})

    assert status.is_synced is False
    assert status.last_synced_at is None


@pytest.mark.parametrize(
    ("started_ago", "expected"),
    [
        (INTERVAL * 2, True),  # 경계: 주기 × 배수와 같으면 아직 동기화됨
        (INTERVAL * 2 + timedelta(seconds=1), False),  # 경계 초과: 수집 멈춤
    ],
)
async def test_마지막_수집_시작이_주기_배수를_넘으면_수집_멈춤으로_본다(
    started_ago, expected
):
    status = await _order_status(
        {"ORDER": _summary(started_ago=started_ago, synced_ago=started_ago)}
    )

    assert status.is_synced is expected


async def test_배수가_0이면_오래된_성공도_동기화됨으로_본다():
    a_year_ago = timedelta(days=365)

    status = await _order_status(
        {"ORDER": _summary(started_ago=a_year_ago, synced_ago=a_year_ago)},
        stale_multiplier=0,
    )

    assert status.is_synced is True


async def test_배수가_0이어도_실패는_동기화_안_됨이다():
    status = await _order_status(
        {"ORDER": _summary(completed_status="FAILED")}, stale_multiplier=0
    )

    assert status.is_synced is False
