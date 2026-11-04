from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from app.modules.delay_analysis.infrastructure.analysis_enums import TriggerReason

"""
    지연 에피소드 판정 (할 일 9 지연 감지, GLOW-16)
      - 선적의 일정 스냅샷이 새로 들어올 때마다, 직전 스냅샷과 비교해 에피소드 시작·종료와 후속 AUTO 실행 사유를 정합니다.
        규칙은 serviceflow v6.2 4절 "지연 에피소드와 분석 실행" 표와 검증 엔진 episodes.py 와 같습니다.

          열린 에피소드 없음 · 입고 전 · 예상 3PL 입고일 > 계획 3PL 입고일  → 에피소드 시작 + EPISODE_STARTED 실행
          열림 · 입고 완료(RECEIVED)                                       → 종료(RECEIVED), 실행 없음
          열림 · 예상일 ≤ 계획일                                           → DELAY_RECOVERED 실행 + 종료(RECOVERED)
          열림 · 예상일 또는 단계가 바뀜 → 지연 일수 변화를 먼저 봄       → SCHEDULE_WORSENED / SCHEDULE_IMPROVED,
                                            지연 일수가 같고 단계만 바뀜 → STAGE_CHANGED
          열림 · 예상일·단계 모두 같음                                     → 아무것도 하지 않음

      - 계획 3PL 입고일은 선적의 저장된 값(새 선적일 때 한 번 정함, serviceflow 1절)입니다.
      - 초기 적재(고객사의 첫 SHIPMENT 수집)의 기준 스냅샷은 호출하는 쪽(저장 단계)이 넣지 않습니다(결정 1-10).
        이 함수는 그 신호를 받지 않고, previous 가 없다는 것만으로 초기 적재로 보지 않습니다 (docs/analysis/delay-detection.md 4절).
      - DB 를 읽지 않는 순수 함수입니다. 에피소드 저장(episode_no·dedupe_key)과 실행 생성 연결은
        0005·create_analysis_run 뒤에 붙입니다.
"""

RECEIVED = "RECEIVED"


class EpisodeAction(StrEnum):
    START = "START"  # 에피소드 시작 + 1번째 AUTO 실행
    FOLLOW_UP = "FOLLOW_UP"  # 같은 에피소드의 후속 AUTO 실행
    CLOSE_RECOVERED = "CLOSE_RECOVERED"  # DELAY_RECOVERED 실행 + 종료
    CLOSE_RECEIVED = "CLOSE_RECEIVED"  # 종료, 실행 없음


class CloseReason(StrEnum):
    """delay_events.close_reason (delay_events_ck5)"""

    RECOVERED = "RECOVERED"
    RECEIVED = "RECEIVED"


@dataclass(frozen=True)
class ScheduleObservation:
    """선적 일정 스냅샷 1건에서 판정에 쓰는 값"""

    stage: str
    expected_3pl_inbound_date: date


@dataclass(frozen=True)
class EpisodeDecision:
    action: EpisodeAction
    trigger_reason: (
        TriggerReason | None
    )  # 만들 AUTO 실행의 생성 사유. 입고 종료는 실행이 없어 None
    close_reason: CloseReason | None = None


def decide_episode(
    *,
    planned_3pl_inbound_date: date,
    current: ScheduleObservation,
    previous: ScheduleObservation | None,
    episode_open: bool,
) -> EpisodeDecision | None:
    """
    이번 스냅샷(current)으로 할 일을 정합니다. 할 일이 없으면 None.
    열린 에피소드가 있으면 previous(직전 스냅샷)가 반드시 있어야 합니다 (에피소드를 연 스냅샷이 있으므로).
    """
    planned = planned_3pl_inbound_date
    expected = current.expected_3pl_inbound_date
    if not episode_open:
        if current.stage != RECEIVED and expected > planned:
            return EpisodeDecision(EpisodeAction.START, TriggerReason.EPISODE_STARTED)
        return None

    if current.stage == RECEIVED:
        return EpisodeDecision(EpisodeAction.CLOSE_RECEIVED, None, CloseReason.RECEIVED)
    if expected <= planned:
        return EpisodeDecision(
            EpisodeAction.CLOSE_RECOVERED,
            TriggerReason.DELAY_RECOVERED,
            CloseReason.RECOVERED,
        )
    if previous is None:
        raise ValueError("열린 에피소드인데 직전 스냅샷이 없습니다")
    if current == previous:
        return None
    now_days = (expected - planned).days
    before_days = (previous.expected_3pl_inbound_date - planned).days
    if now_days > before_days:
        reason = TriggerReason.SCHEDULE_WORSENED
    elif now_days < before_days:
        reason = TriggerReason.SCHEDULE_IMPROVED
    else:
        reason = TriggerReason.STAGE_CHANGED
    return EpisodeDecision(EpisodeAction.FOLLOW_UP, reason)
