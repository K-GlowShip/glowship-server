import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from app.modules.delay_analysis.application.detection.episode_rules import (
    CloseReason,
    EpisodeAction,
    ScheduleObservation,
    decide_episode,
)
from app.modules.delay_analysis.infrastructure.analysis_enums import TriggerReason
from app.modules.delay_analysis.infrastructure.repository.delay_event_write_repository import (
    DelayEventWriteRepository,
    NewDelayEvent,
    RunSnapshot,
)

"""
    지연 에피소드 저장 서비스 (할 일 9 지연 감지, GLOW-16, docs/analysis/delay-detection.md 3·4절)
      - SHIPMENT 수집이 일정 스냅샷을 남긴 같은 트랜잭션 안에서, 수집 완료 기록 뒤·STALE 판정 전에 부른다.
      - 초기 적재(고객사의 첫 SHIPMENT 수집, 결정 1-10)면 아무것도 하지 않는다. 선적을 0건 적재한 수집은 세지 않는다. 그 수집의 스냅샷은 기준 스냅샷이고
        다음 수집부터 직전 스냅샷으로 쓰인다.
      - 판정과 종료를 나눈다. 호출 순서: detect_after_sync → (AUTO 실행 생성·이전 실행 STALE, GLOW-18) → close_episodes.
          detect_after_sync: 스냅샷마다 decide_episode 로 판정한다. START 만 바로 저장하고
                             (episode_no = 선적의 마지막 번호 + 1, 감지 시각 = 스냅샷 관측 시각),
                             종료(CLOSE_RECEIVED·CLOSE_RECOVERED)는 결과로만 돌려준다. FOLLOW_UP 은 저장하지 않는다.
          close_episodes:   결과 중 종료할 것을 닫는다 (closed_at = 스냅샷 관측 시각, close_reason).
        회복으로 닫을 때 이전 실행을 STALE(DELAY_SCHEDULE_CHANGED)로 바꾸는 mark_runs_stale 은 닫힌 에피소드의 실행을
        동결로 보고 건너뛰므로(stale_service._unfrozen), 반드시 닫기 전에 불러야 한다. 그래서 종료를 따로 둔다.
        입고(RECEIVED)로 닫을 때는 실행을 만들지 않고 이전 실행도 STALE 로 바꾸지 않는다 (serviceflow 4절, 확정 데이터 같음).
      - 판정 결과(지연·생성 사유·원인 스냅샷)를 돌려준다. AUTO 실행 생성(create_analysis_run, 개발자 3)은
        이 결과로 따로 붙인다 (CLOSE_RECEIVED 는 실행이 없어 trigger_reason 이 None).
"""


@dataclass(frozen=True)
class EpisodeOutcome:
    """스냅샷 1건의 판정 결과와 그 지연. close_reason 이 있으면 close_episodes 가 닫을 대상이다."""

    action: EpisodeAction
    delay_event_id: uuid.UUID
    shipment_id: uuid.UUID
    trigger_snapshot_id: uuid.UUID
    trigger_reason: TriggerReason | None
    observed_at: datetime
    close_reason: CloseReason | None = None


def _previous_observation(snapshot: RunSnapshot) -> ScheduleObservation | None:
    if snapshot.previous_snapshot_id is None:
        return None
    return ScheduleObservation(
        snapshot.previous_stage, snapshot.previous_expected_3pl_inbound_date
    )


class EpisodeDetectionService:
    def __init__(self, repo: DelayEventWriteRepository):
        self.repo = repo

    async def detect_after_sync(
        self, company_id: uuid.UUID, data_sync_run_id: uuid.UUID
    ) -> list[EpisodeOutcome]:
        """판정하고 시작만 저장한다. 종료는 결과로만 돌려준다 (close_episodes 가 닫음)."""
        if not await self.repo.has_prior_applied_shipment_sync(
            company_id, data_sync_run_id
        ):
            return []  # 초기 적재 (결정 1-10)
        snapshots = await self.repo.find_run_snapshots(company_id, data_sync_run_id)
        episodes = await self.repo.find_shipment_episodes(
            company_id, [s.shipment_id for s in snapshots]
        )
        outcomes = []
        for snapshot in snapshots:
            current = episodes.get(snapshot.shipment_id)
            open_id = current.open_delay_event_id if current else None
            decision = decide_episode(
                planned_3pl_inbound_date=snapshot.planned_3pl_inbound_date,
                current=ScheduleObservation(
                    snapshot.stage, snapshot.expected_3pl_inbound_date
                ),
                previous=_previous_observation(snapshot),
                episode_open=open_id is not None,
            )
            if decision is None:
                continue
            if decision.action == EpisodeAction.START:
                open_id = uuid.uuid7()
                await self.repo.insert_delay_event(
                    company_id,
                    NewDelayEvent(
                        delay_event_id=open_id,
                        shipment_id=snapshot.shipment_id,
                        episode_no=(current.last_episode_no if current else 0) + 1,
                        detected_snapshot_id=snapshot.snapshot_id,
                        previous_snapshot_id=snapshot.previous_snapshot_id,
                        stage=snapshot.stage,
                        planned_3pl_inbound_date=snapshot.planned_3pl_inbound_date,
                        expected_3pl_inbound_date=snapshot.expected_3pl_inbound_date,
                        detected_at=snapshot.observed_at,
                    ),
                )
            outcomes.append(
                EpisodeOutcome(
                    action=decision.action,
                    delay_event_id=open_id,
                    shipment_id=snapshot.shipment_id,
                    trigger_snapshot_id=snapshot.snapshot_id,
                    trigger_reason=decision.trigger_reason,
                    observed_at=snapshot.observed_at,
                    close_reason=decision.close_reason,
                )
            )
        return outcomes

    async def close_episodes(
        self, company_id: uuid.UUID, outcomes: Iterable[EpisodeOutcome]
    ) -> None:
        """판정 결과 중 종료(입고·회복)를 닫는다. 회복은 이전 실행 STALE 처리 뒤에 불러야 한다 (모듈 설명)."""
        for outcome in outcomes:
            if outcome.close_reason is None:
                continue
            await self.repo.close_delay_event(
                company_id,
                outcome.delay_event_id,
                closed_at=outcome.observed_at,
                close_reason=outcome.close_reason.value,
            )
