import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.locks import lock_analysis_run, lock_analysis_runs
from app.modules.delay_analysis.application.stale.relevance import (
    group_by_applied_seq,
    relevant_reasons,
    relevant_reasons_in_progress,
)
from app.modules.delay_analysis.infrastructure.analysis_run import (
    MULTIPLE_RELEVANT_CHANGES,
    STALE_REASON_CODES,
    AnalysisRun,
)
from app.modules.delay_analysis.infrastructure.repository.stale_change_repository import (
    ScopedChange,
    StaleChangeRepository,
)

"""
    STALE 판정·기록 (D-13 의존 입력 기반 STALE, 정책 결정 ST-3 C안)
      - 1차: 수집의 변경 기록이 실행의 의존 범위에 해당하는지 (relevance.py)
      - 2차: 해당하면 판정 서명을 다시 계산해 바뀌었을 때만 STALE 로 기록합니다.
        재계산은 분석 계산 코어가 필요하므로 SignatureRecalculator 로 주입받습니다.
        주입되지 않으면 1차에서 걸린 실행을 STALE 로 봅니다 (선택 전 놓치지 않는 쪽).
      - 잠금: 호출 측이 L1(고객사)을 잡은 트랜잭션 안에서 부르고, 여기서 L2(실행)를 실행 ID 순서로 잡습니다.
      - 대상: 현재 실행 버전이고 진행 중·READY_FOR_REVIEW·NO_RISK 이며 지연 에피소드가 열린 실행.
        FINALIZED·FAILED·ON_HOLD·NO_DELAY·STALE 과 종료된 에피소드의 실행은 바꾸지 않습니다.
"""

IN_PROGRESS_STATUSES = (
    "DETECTED",
    "VALIDATING",
    "ANALYZING_IMPACT",
    "ANALYZING_OPTIONS",
    "VALIDATING_OUTPUT",
)
STALE_TARGET_STATUSES = (*IN_PROGRESS_STATUSES, "READY_FOR_REVIEW", "NO_RISK")


class SignatureRecalculator(Protocol):
    async def signature_changed(
        self, run: AnalysisRun, changes: Sequence[ScopedChange]
    ) -> bool:
        """changes 까지 반영한 입력으로 판정 서명을 다시 계산해 실행의 서명과 다르면 True"""
        ...


def mark_stale(
    run: AnalysisRun,
    reason_codes: Iterable[str],
    detected_seq: int | None,
    detected_at: datetime,
) -> None:
    """실행을 STALE 로 바꾸고 사유를 기록합니다. 사유가 2개 이상이면 주 사유는 MULTIPLE_RELEVANT_CHANGES 입니다."""
    codes = sorted(set(reason_codes))
    if not codes or not set(codes) <= set(STALE_REASON_CODES):
        raise ValueError(f"알 수 없는 STALE 사유: {codes}")
    if (codes == ["TIME_ELAPSED"]) != (detected_seq is None):
        raise ValueError("적용 순번은 TIME_ELAPSED 일 때만 비웁니다")
    run.status = "STALE"
    run.stale_primary_reason = (
        codes[0] if len(codes) == 1 else MULTIPLE_RELEVANT_CHANGES
    )
    run.stale_reason_codes = codes
    run.stale_detected_seq = detected_seq
    run.stale_detected_at = detected_at


class StaleJudgmentService:
    def __init__(
        self,
        db: AsyncSession,
        change_repo: StaleChangeRepository,
        recalculator: SignatureRecalculator | None = None,
    ):
        self.db = db
        self.change_repo = change_repo
        self.recalculator = recalculator

    async def judge_after_sync(
        self, company_id: uuid.UUID, applied_seq: int, detected_at: datetime
    ) -> list[AnalysisRun]:
        """
        수집 적재 트랜잭션 안에서, 방금 적용한 수집(applied_seq)의 변경으로 대상 실행을 판정합니다.
        호출 측이 L1 을 잡고 변경 기록·적용 순번을 저장한 뒤 부릅니다. STALE 로 바뀐 실행을 반환합니다.
        """
        run_ids = await self.change_repo.find_target_run_ids(company_id, applied_seq)
        if not run_ids:
            return []
        runs = await lock_analysis_runs(self.db, run_ids)
        changes = await self.change_repo.find_changes_after(
            company_id, applied_seq - 1, applied_seq
        )
        if not changes:
            return []
        staled = []
        for run in runs:
            if _is_target(run) and await self._judge(run, changes, detected_at):
                staled.append(run)
        await self.db.flush()
        return staled

    async def recheck_run(
        self, analysis_run_id: uuid.UUID, up_to_seq: int, detected_at: datetime
    ) -> AnalysisRun | None:
        """
        분석 완료 처리 때, 의존 범위가 확정된 실행을 입력 순번 이후 변경 전체로 다시 판정합니다 (Q-13a).
        STALE 로 바뀌면 그 실행을, 아니면 None 을 반환합니다.
        """
        run = await lock_analysis_run(self.db, analysis_run_id)
        if run is None or not _is_target(run):
            return None
        changes = await self.change_repo.find_changes_after(
            run.company_id, run.input_seq, up_to_seq
        )
        if not await self._judge(run, changes, detected_at):
            return None
        await self.db.flush()
        return run

    async def mark_runs_stale(
        self,
        analysis_run_ids: Iterable[uuid.UUID],
        reason_code: str,
        detected_seq: int,
        detected_at: datetime,
    ) -> list[AnalysisRun]:
        """
        사유를 이미 아는 실행을 STALE 로 바꿉니다.
        예: 지연 선적 일정·단계가 바뀌어 새 AUTO 실행을 만들 때 이전 실행을
        DELAY_SCHEDULE_CHANGED·DELAY_STAGE_CHANGED 로 (D-08). 대상이 아닌 상태의 실행은 그대로 둡니다.
        """
        runs = await lock_analysis_runs(self.db, analysis_run_ids)
        staled = [run for run in runs if run.status in STALE_TARGET_STATUSES]
        for run in staled:
            mark_stale(run, [reason_code], detected_seq, detected_at)
        await self.db.flush()
        return staled

    async def _judge(
        self, run: AnalysisRun, changes: Iterable[ScopedChange], detected_at: datetime
    ) -> bool:
        """실행 입력 순번 뒤의 변경을 수집 순서대로 보고, 처음 서명이 바뀐 수집에서 STALE 로 기록합니다."""
        own_changes = [c for c in changes if c.applied_seq > run.input_seq]
        seen: list[ScopedChange] = []
        for seq, group in group_by_applied_seq(own_changes).items():
            seen.extend(group)
            reasons = await self._relevant_reasons(run, group)
            if reasons and await self._signature_changed(run, seen):
                mark_stale(run, reasons, seq, detected_at)
                return True
        return False

    async def _relevant_reasons(
        self, run: AnalysisRun, changes: list[ScopedChange]
    ) -> list[str]:
        if run.dependency_scope is not None:
            return relevant_reasons(run.dependency_scope, changes)
        # 의존 범위가 아직 없는 진행 중 실행은 지연 선적 SKU 전체로 보수 판정합니다
        shipment = await self.change_repo.find_delayed_shipment_skus(
            run.company_id, run.delay_event_id
        )
        if shipment is None:
            return []
        delayed_shipment_id, k_shipment = shipment
        return relevant_reasons_in_progress(
            k_shipment,
            delayed_shipment_id,
            run.started_at.astimezone(UTC).date(),  # 입력 확정일 D
            changes,
        )

    async def _signature_changed(
        self, run: AnalysisRun, changes: Sequence[ScopedChange]
    ) -> bool:
        # 진행 중 실행은 서명이 아직 없으므로 1차 결과로 판정합니다
        if self.recalculator is None or run.status in IN_PROGRESS_STATUSES:
            return True
        return await self.recalculator.signature_changed(run, changes)


def _is_target(run: AnalysisRun) -> bool:
    """잠근 뒤 다시 확인: 그 사이 상태가 바뀌었거나 새 버전이 생긴 실행은 건너뜁니다."""
    return run.is_latest and run.status in STALE_TARGET_STATUSES
