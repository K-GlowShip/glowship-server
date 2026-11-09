import uuid
from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.delay_analysis.application.detection.episode_rules import (
    EpisodeAction,
)
from app.modules.delay_analysis.application.detection.episode_service import (
    EpisodeOutcome,
)
from app.modules.delay_analysis.application.run_creation.run_creation_service import (
    create_analysis_run,
    record_input_data_versions,
)
from app.modules.delay_analysis.application.stale.stale_service import (
    StaleJudgmentService,
)
from app.modules.delay_analysis.infrastructure.analysis_enums import (
    StaleReasonCode,
    TriggerReason,
)
from app.modules.delay_analysis.infrastructure.analysis_run import AnalysisRun
from app.modules.delay_analysis.infrastructure.repository.stale_change_repository import (
    StaleChangeRepository,
)

"""
    지연 감지 → AUTO 분석 실행 생성 (GLOW-18, 이슈 #34, serviceflow 4·5.2절)
      - SHIPMENT 수집 적용 트랜잭션 안(L1 을 이미 잡음)에서, 에피소드 판정(detect_after_sync) 뒤·에피소드 종료(close_episodes) 전에 부른다.
      - 판정 결과마다:
          시작(START, EPISODE_STARTED)          → 실행 v1 생성
          후속(FOLLOW_UP, 악화·개선·단계 변경)    → 이전 실행 STALE 뒤 새 버전 생성
          회복 종료(CLOSE_RECOVERED, DELAY_RECOVERED) → 이전 실행 STALE 뒤 새 버전 생성(분석이 NO_DELAY 로 끝냄), 그 뒤 에피소드 종료
          입고 종료(CLOSE_RECEIVED)              → 실행 없음, 이전 실행도 그대로(serviceflow 4절)
      - 이전 실행 STALE 사유: 단계 변경(STAGE_CHANGED)이면 DELAY_STAGE_CHANGED, 그 밖은 DELAY_SCHEDULE_CHANGED
        (엔진 하네스 run_s1_s5.py new_run, 확정 DB 와 같음). 감지 순번 = 이 수집의 적용 순번, 감지 시각 = 수집 완료 시각.
        에피소드를 닫기 전에 해야 한다(닫힌 에피소드의 실행은 동결로 보고 건너뜀, stale_service._unfrozen).
      - 실행 시각 = 이 수집의 확정 시작 시각(이슈 #50: 고객사 마지막 적재 수집 시작). 원인 스냅샷 = 판정한 스냅샷.
        같은 지연·같은 원인 스냅샷의 AUTO 실행은 create_analysis_run 이 한 건만 만든다.
      - 분석 보류(GLOW-15)는 여기서 판정하지 않는다. 실행은 DETECTED 로 남고, 보류는 분석을 시작할 때 엔진 입력
        (#53 EngineHold, 같은 "차단 검증 오류" 조건)이 정한다(사용자 결정 11-06).
      - 분석 시작(DETECTED → 분석)은 이 서비스 밖이다(GLOW-128, #51).
"""

_STAGE_REASON = {TriggerReason.STAGE_CHANGED: StaleReasonCode.DELAY_STAGE_CHANGED}


def previous_run_stale_reason(trigger_reason: TriggerReason) -> StaleReasonCode:
    """후속 실행을 만들 때 이전 실행의 STALE 사유 (엔진 하네스와 같음)"""
    return _STAGE_REASON.get(trigger_reason, StaleReasonCode.DELAY_SCHEDULE_CHANGED)


class AutoRunService:
    def __init__(self, db: AsyncSession, stale: StaleJudgmentService | None = None):
        self.db = db
        self.stale = stale or StaleJudgmentService(db, StaleChangeRepository(db))

    async def create_after_sync(
        self,
        company_id: uuid.UUID,
        outcomes: Iterable[EpisodeOutcome],
        *,
        applied_seq: int,
        started_at: datetime,
        finished_at: datetime,
    ) -> list[uuid.UUID]:
        """판정 결과마다 AUTO 실행을 만든다(실행이 생기는 결과만). 새로 만든 실행 ID 를 돌려준다."""
        created = []
        for outcome in outcomes:
            if outcome.trigger_reason is None:  # 입고 종료
                continue
            trigger_reason = outcome.trigger_reason
            if outcome.action != EpisodeAction.START:
                previous = await self.db.scalar(
                    select(AnalysisRun.analysis_run_id).where(
                        AnalysisRun.delay_event_id == outcome.delay_event_id,
                        AnalysisRun.is_latest,
                    )
                )
                if previous is not None:
                    await self.stale.mark_runs_stale(
                        [previous],
                        previous_run_stale_reason(outcome.trigger_reason),
                        detected_seq=applied_seq,
                        detected_at=finished_at,
                    )
                elif outcome.action == EpisodeAction.CLOSE_RECOVERED:
                    continue  # 분석한 적 없는 지연이 회복으로 닫힘 → 분석할 지연이 없다
                else:
                    # 실행이 없는 열린 지연(시작 때 실행을 만들지 못함)의 후속 판정 → 이 판정으로 첫 실행(v1)을 만든다.
                    # 첫 실행은 EPISODE_STARTED 로만 만들 수 있다(ck18, #63 리뷰 EPISODE_START_MISMATCH)
                    trigger_reason = TriggerReason.EPISODE_STARTED
            creation = await create_analysis_run(
                self.db,
                company_id=company_id,
                delay_event_id=outcome.delay_event_id,
                trigger_reason=trigger_reason,
                trigger_shipment_schedule_snapshot_id=outcome.trigger_snapshot_id,
                started_at=started_at,
            )
            if creation.created:
                await record_input_data_versions(self.db, creation.run)
                created.append(creation.run.analysis_run_id)
        return created
