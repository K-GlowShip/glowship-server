import uuid
from datetime import UTC, date, datetime

import pytest

from app.modules.delay_analysis.application.detection.episode_rules import (
    EpisodeAction,
)
from app.modules.delay_analysis.application.detection.episode_service import (
    EpisodeDetectionService,
    EpisodeOutcome,
)
from app.modules.delay_analysis.infrastructure.analysis_enums import TriggerReason
from app.modules.delay_analysis.infrastructure.repository.delay_event_write_repository import (
    RunSnapshot,
    ShipmentEpisodes,
    dedupe_key,
)

"""
    지연 에피소드 저장 서비스 단위 테스트 (DB 없음, docs/analysis/delay-detection.md 4절)
      - 초기 적재(첫 SHIPMENT 수집)는 건너뜀, 시작은 다음 번호·dedupe_key·감지 시각 = 관측 시각,
        종료 2가지는 열린 에피소드의 closed_at·close_reason, 후속은 저장 없이 사유만 돌려줌
"""

COMPANY_ID = uuid.uuid7()
RUN_ID = uuid.uuid7()
SHIPMENT_ID = uuid.uuid7()
OPEN_EVENT_ID = uuid.uuid7()
OBSERVED_AT = datetime(2026, 1, 10, 0, 3, 20, tzinfo=UTC)
PLANNED = date(2026, 1, 20)


def _snapshot(stage="AT_SEA", expected=date(2026, 1, 25), previous=None) -> RunSnapshot:
    previous_stage, previous_expected = previous or (None, None)
    return RunSnapshot(
        snapshot_id=uuid.uuid7(),
        shipment_id=SHIPMENT_ID,
        observed_at=OBSERVED_AT,
        stage=stage,
        expected_3pl_inbound_date=expected,
        planned_3pl_inbound_date=PLANNED,
        previous_snapshot_id=uuid.uuid7() if previous else None,
        previous_stage=previous_stage,
        previous_expected_3pl_inbound_date=previous_expected,
    )


class FakeWriteRepository:
    def __init__(self, *, prior=True, snapshots=(), episodes=None):
        self.prior = prior
        self.snapshots = list(snapshots)
        self.episodes = episodes or {}
        self.inserted: list = []
        self.closed: list = []
        self.snapshot_calls = 0

    async def has_prior_applied_shipment_sync(self, company_id, data_sync_run_id):
        return self.prior

    async def find_run_snapshots(self, company_id, data_sync_run_id):
        self.snapshot_calls += 1
        return self.snapshots

    async def find_shipment_episodes(self, company_id, shipment_ids):
        return {k: v for k, v in self.episodes.items() if k in shipment_ids}

    async def insert_delay_event(self, company_id, event):
        self.inserted.append(event)

    async def close_delay_event(self, company_id, delay_event_id, **kwargs):
        self.closed.append((delay_event_id, kwargs))


async def test_초기_적재_수집은_스냅샷을_읽지도_않고_건너뛴다():
    repo = FakeWriteRepository(prior=False, snapshots=[_snapshot()])

    outcomes = await EpisodeDetectionService(repo).detect_after_sync(COMPANY_ID, RUN_ID)

    assert outcomes == []
    assert repo.snapshot_calls == 0 and repo.inserted == []


async def test_지연이면_다음_번호로_에피소드를_연다():
    snapshot = _snapshot(previous=("AT_SEA", date(2026, 1, 18)))
    repo = FakeWriteRepository(
        snapshots=[snapshot],
        episodes={
            SHIPMENT_ID: ShipmentEpisodes(last_episode_no=2, open_delay_event_id=None)
        },
    )

    outcomes = await EpisodeDetectionService(repo).detect_after_sync(COMPANY_ID, RUN_ID)

    [event] = repo.inserted
    assert event.episode_no == 3
    assert event.detected_snapshot_id == snapshot.snapshot_id
    assert event.previous_snapshot_id == snapshot.previous_snapshot_id
    assert (
        event.stage,
        event.planned_3pl_inbound_date,
        event.expected_3pl_inbound_date,
    ) == (
        "AT_SEA",
        PLANNED,
        date(2026, 1, 25),
    )
    assert event.detected_at == OBSERVED_AT
    assert outcomes == [
        EpisodeOutcome(
            action=EpisodeAction.START,
            delay_event_id=event.delay_event_id,
            shipment_id=SHIPMENT_ID,
            trigger_snapshot_id=snapshot.snapshot_id,
            trigger_reason=TriggerReason.EPISODE_STARTED,
        )
    ]
    assert dedupe_key(SHIPMENT_ID, 3) == f"{SHIPMENT_ID}:EP3"


async def test_운영_중_처음_관측된_선적도_지연이면_1번으로_연다():
    repo = FakeWriteRepository(snapshots=[_snapshot()])

    await EpisodeDetectionService(repo).detect_after_sync(COMPANY_ID, RUN_ID)

    assert [e.episode_no for e in repo.inserted] == [1]
    assert repo.inserted[0].previous_snapshot_id is None


@pytest.mark.parametrize(
    ("stage", "expected", "close_reason", "trigger_reason"),
    [
        ("RECEIVED", date(2026, 1, 25), "RECEIVED", None),
        ("AT_PORT", date(2026, 1, 19), "RECOVERED", TriggerReason.DELAY_RECOVERED),
    ],
)
async def test_열린_에피소드를_입고나_회복으로_닫는다(
    stage, expected, close_reason, trigger_reason
):
    snapshot = _snapshot(
        stage=stage, expected=expected, previous=("AT_SEA", date(2026, 1, 25))
    )
    repo = FakeWriteRepository(
        snapshots=[snapshot],
        episodes={SHIPMENT_ID: ShipmentEpisodes(1, OPEN_EVENT_ID)},
    )

    outcomes = await EpisodeDetectionService(repo).detect_after_sync(COMPANY_ID, RUN_ID)

    assert repo.closed == [
        (OPEN_EVENT_ID, {"closed_at": OBSERVED_AT, "close_reason": close_reason})
    ]
    assert repo.inserted == []
    assert [(o.delay_event_id, o.trigger_reason) for o in outcomes] == [
        (OPEN_EVENT_ID, trigger_reason)
    ]


async def test_후속_변경은_저장하지_않고_사유만_돌려준다():
    snapshot = _snapshot(
        expected=date(2026, 1, 28), previous=("AT_SEA", date(2026, 1, 25))
    )
    repo = FakeWriteRepository(
        snapshots=[snapshot],
        episodes={SHIPMENT_ID: ShipmentEpisodes(1, OPEN_EVENT_ID)},
    )

    outcomes = await EpisodeDetectionService(repo).detect_after_sync(COMPANY_ID, RUN_ID)

    assert repo.inserted == [] and repo.closed == []
    assert [(o.action, o.delay_event_id, o.trigger_reason) for o in outcomes] == [
        (EpisodeAction.FOLLOW_UP, OPEN_EVENT_ID, TriggerReason.SCHEDULE_WORSENED)
    ]


async def test_지연이_아니면_아무것도_하지_않는다():
    repo = FakeWriteRepository(snapshots=[_snapshot(expected=date(2026, 1, 19))])

    outcomes = await EpisodeDetectionService(repo).detect_after_sync(COMPANY_ID, RUN_ID)

    assert outcomes == [] and repo.inserted == [] and repo.closed == []
