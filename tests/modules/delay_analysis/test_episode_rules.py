from datetime import date

import pytest

from app.modules.delay_analysis.application.detection.episode_rules import (
    CloseReason,
    EpisodeAction,
    EpisodeDecision,
    ScheduleObservation,
    decide_episode,
)
from app.modules.delay_analysis.infrastructure.analysis_enums import TriggerReason

"""
    지연 에피소드 판정 (DB 없음). serviceflow v6.2 4절 "지연 에피소드와 분석 실행" 표의 행마다 하나씩.
"""

PLANNED = date(2026, 1, 20)


def _obs(stage: str, day: int) -> ScheduleObservation:
    return ScheduleObservation(stage, date(2026, 1, day))


def _decide(current, previous=None, *, open_):
    return decide_episode(
        planned_3pl_inbound_date=PLANNED,
        current=current,
        previous=previous,
        episode_open=open_,
    )


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        # 입고 전 · 예상일 > 계획일 → 시작
        (
            _obs("AT_SEA", 21),
            EpisodeDecision(EpisodeAction.START, TriggerReason.EPISODE_STARTED),
        ),
        (
            _obs("PRE_DEPARTURE", 25),
            EpisodeDecision(EpisodeAction.START, TriggerReason.EPISODE_STARTED),
        ),
        # 예상일 = 계획일 (지연 아님)
        (_obs("AT_SEA", 20), None),
        # 이미 입고 완료면 늦었어도 시작하지 않음
        (_obs("RECEIVED", 25), None),
    ],
)
def test_without_open_episode(current, expected):
    assert _decide(current, open_=False) == expected


@pytest.mark.parametrize(
    ("previous", "current", "expected"),
    [
        # 지연 일수 늘어남
        (
            _obs("AT_SEA", 21),
            _obs("AT_SEA", 23),
            EpisodeDecision(EpisodeAction.FOLLOW_UP, TriggerReason.SCHEDULE_WORSENED),
        ),
        # 지연 일수 줄어듦 (아직 계획일 뒤)
        (
            _obs("AT_SEA", 23),
            _obs("AT_SEA", 21),
            EpisodeDecision(EpisodeAction.FOLLOW_UP, TriggerReason.SCHEDULE_IMPROVED),
        ),
        # 지연 일수 같고 단계만 바뀜
        (
            _obs("AT_SEA", 21),
            _obs("AT_PORT", 21),
            EpisodeDecision(EpisodeAction.FOLLOW_UP, TriggerReason.STAGE_CHANGED),
        ),
        # 단계와 예상일이 같이 바뀌면 지연 일수 변화가 먼저
        (
            _obs("AT_SEA", 21),
            _obs("AT_PORT", 22),
            EpisodeDecision(EpisodeAction.FOLLOW_UP, TriggerReason.SCHEDULE_WORSENED),
        ),
        # 예상일이 계획일 이내로 돌아옴 → DELAY_RECOVERED 실행 + 종료
        (
            _obs("AT_SEA", 21),
            _obs("AT_SEA", 20),
            EpisodeDecision(
                EpisodeAction.CLOSE_RECOVERED,
                TriggerReason.DELAY_RECOVERED,
                CloseReason.RECOVERED,
            ),
        ),
        # 입고 완료 → 종료, 실행 없음 (늦게 입고돼도)
        (
            _obs("AT_PORT", 22),
            _obs("RECEIVED", 22),
            EpisodeDecision(EpisodeAction.CLOSE_RECEIVED, None, CloseReason.RECEIVED),
        ),
        # 아무것도 바뀌지 않음
        (_obs("AT_SEA", 21), _obs("AT_SEA", 21), None),
    ],
)
def test_with_open_episode(previous, current, expected):
    assert _decide(current, previous, open_=True) == expected


def test_received_closes_before_recovery_check():
    """입고 완료는 예상일이 계획일 이내여도 RECOVERED 가 아니라 RECEIVED 종료"""
    decision = _decide(_obs("RECEIVED", 19), _obs("AT_PORT", 21), open_=True)

    assert decision.close_reason == CloseReason.RECEIVED


def test_open_episode_requires_previous_snapshot():
    with pytest.raises(ValueError):
        _decide(_obs("AT_SEA", 22), None, open_=True)
