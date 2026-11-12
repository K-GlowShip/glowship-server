import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.locks import lock_analysis_run, lock_company
from app.modules.delay_analysis.application.exception.analysis_run_creation_exceptions import (
    AnalysisRunCreationError,
    RunCreationErrorCode,
)
from app.modules.delay_analysis.application.run_creation.run_code import (
    basis_date,
    day_prefix,
    next_run_code,
)
from app.modules.delay_analysis.infrastructure.analysis_enums import (
    AnalysisRunStatus,
    TriggerReason,
    TriggerType,
)
from app.modules.delay_analysis.infrastructure.analysis_run import AnalysisRun
from app.modules.delay_analysis.infrastructure.analysis_run_data_version import (
    AnalysisRunDataVersion,
)
from app.modules.delay_analysis.infrastructure.repository.run_creation_repository import (
    RunCreationRepository,
)
from app.modules.delay_analysis.infrastructure.repository.stale_change_repository import (
    StaleChangeRepository,
)

"""
    분석 실행 생성 = 입력 확정 (명세 v6.2 3·8절, serviceflow 5.2 처리 단계 1, GLOW-99)
      - 부르는 곳: 지연 감지(수집 적재, AUTO 사유 5종), 재분석 API(USER_REANALYSIS).
      - 호출하는 쪽이 고객사 잠금(L1)을 잡은 트랜잭션 안에서 부른다. 안전장치로 여기서도 한 번 더 잡는다
        (pg_advisory_xact_lock 은 같은 트랜잭션에서 다시 잡아도 막히지 않음). 커밋은 호출하는 쪽이 한다.
      - 이전 실행은 is_latest 만 내린다. 이전 실행의 STALE 기록(후속 AUTO 일 때)은 B 가 같은 L1 안에서
        mark_runs_stale 로 한다(PR #11 리뷰 합의). 사용자 재분석은 STALE 사유가 아니다.
      - 같은 지연·같은 트리거 스냅샷의 AUTO 실행은 1건만 만든다. 이미 있으면 새로 만들지 않고 그 실행을 돌려준다.
      - 값을 바꾸기 전에 모든 검사를 끝낸다. 실패하면 DB 는 바뀌지 않은 채 AnalysisRunCreationError 가 난다.
      - 기록하는 값과 규칙(데이터 팀 엔진 snapshot.build 와 같음)
          started_at: L1 을 잡은 뒤의 DB 시각(clock_timestamp). 잠금 순서와 시각 순서가 같아진다.
                      부르는 쪽이 started_at 을 넘기면 그 값을 쓴다(이슈 #50: 가상 시각 데모에서 7월 초 시각).
                      부르는 쪽은 L1 을 잡은 뒤 그 값을 구해야 하고, 고객사 마지막 적재 수집 시작(초 단위)보다
                      이르면 RUN_TIME_BEFORE_INPUT 으로 막는다. 엔진은 "시작 ≤ 실행 시각인 적재 수집의 최대 순번"을
                      입력 순번으로 다시 계산하므로(PR #36 check_input_seq), 더 이르면 input_seq 와 어긋나
                      분석 입력을 만들 때 멈춘다. 조용히 올리지 않는 것은 부르는 쪽 실수를 가리지 않기 위해서다.
          input_seq: 고객사의 최대 적용 순번(STALE 판정과 같은 find_max_applied_seq)
          operating_rule_set_id: 기준일(started_at 의 UTC 날짜)에 시작한 버전 중 가장 큰 버전
          run_code: AN-기준일-고객사·기준일별 순번(D14, 0006)
          계산 계약 T14-v1, 배정 규칙 D00-B-b2-v1 (DDL ck34·ck35 가 받는 유일한 값)
      - 데이터 버전(analysis_run_data_versions)은 record_input_data_versions 가 따로 기록한다.
        overlay 에서 입력 검증에 실패한 ON_HOLD 실행은 데이터 버전이 없으므로, 입력 검증(GLOW-100)을 통과한 실행의 결과를 저장할 때 부른다
        (outcome_save·transitions, 데모 시드는 결과를 넣을 때). 실행을 만들 때는 부르지 않는다(2026-11-11, serviceflow 5.2).
"""

CALCULATION_CONTRACT_VERSION = "T14-v1"
ALLOCATION_RULE_VERSION = "D00-B-b2-v1"


@dataclass(frozen=True)
class AnalysisRunCreation:
    run: AnalysisRun  # 새로 만든 실행, 또는 이미 있던 같은 트리거의 AUTO 실행
    created: bool  # False 면 같은 지연·같은 트리거 스냅샷의 AUTO 실행이 이미 있어 아무것도 바꾸지 않았다
    previous_run: (
        AnalysisRun | None
    )  # 새 실행의 부모(is_latest 를 내린 실행). 첫 실행이거나 created=False 면 None


def _trigger_type(
    reason: TriggerReason,
    snapshot_id: uuid.UUID | None,
    requested_by_user_id: uuid.UUID | None,
    request_reason: str | None,
) -> TriggerType:
    """사유로 실행 종류를 정하고, 종류별로 있어야 하는 값·없어야 하는 값을 DDL 규칙대로 확인한다"""
    if reason == TriggerReason.USER_REANALYSIS:
        if requested_by_user_id is None:
            raise AnalysisRunCreationError(
                RunCreationErrorCode.INVALID_TRIGGER,
                "사용자 재분석에는 요청자(requested_by_user_id)가 있어야 합니다 (ck2)",
            )
        if snapshot_id is not None:
            raise AnalysisRunCreationError(
                RunCreationErrorCode.INVALID_TRIGGER,
                "사용자 재분석에는 트리거 스냅샷을 넣지 않습니다 (ck16)",
            )
        return TriggerType.REANALYSIS
    if snapshot_id is None:
        raise AnalysisRunCreationError(
            RunCreationErrorCode.INVALID_TRIGGER,
            f"자동 실행({reason})에는 트리거 스냅샷이 있어야 합니다 (ck16)",
        )
    if requested_by_user_id is not None or request_reason is not None:
        raise AnalysisRunCreationError(
            RunCreationErrorCode.INVALID_TRIGGER,
            "자동 실행에는 요청자·요청 사유를 넣지 않습니다 (ck2·ck4)",
        )
    return TriggerType.AUTO


async def create_analysis_run(
    db: AsyncSession,
    *,
    company_id: uuid.UUID,
    delay_event_id: uuid.UUID,
    trigger_reason: str,
    trigger_shipment_schedule_snapshot_id: uuid.UUID | None = None,
    requested_by_user_id: uuid.UUID | None = None,
    request_reason: str | None = None,
    started_at: datetime | None = None,
) -> AnalysisRunCreation:
    """
    지연 이벤트의 새 분석 실행을 만들어 DETECTED 상태로 돌려준다 (호출하는 쪽이 L1 을 잡고 있어야 한다).
      - started_at: 실행 시각(시간대 있는 값). 없으면 L1 뒤 DB 시각. 있으면 고객사 마지막 적재 수집 시작 이상이어야 한다.
      - 첫 실행(version 1)은 EPISODE_STARTED 로만, 그 뒤 실행은 다른 사유로만 만든다 (ck18).
      - 새 실행의 부모는 지금 현재 실행이고, is_latest 는 새 실행으로 옮긴다.
    """
    try:
        reason = TriggerReason(trigger_reason)
    except ValueError:
        raise AnalysisRunCreationError(
            RunCreationErrorCode.INVALID_TRIGGER,
            f"알 수 없는 실행 사유: {trigger_reason}",
        ) from None
    trigger_type = _trigger_type(
        reason,
        trigger_shipment_schedule_snapshot_id,
        requested_by_user_id,
        request_reason,
    )

    await lock_company(db, company_id)
    repo = RunCreationRepository(db)

    event = await repo.find_delay_event(company_id, delay_event_id)
    if event is None:
        raise AnalysisRunCreationError(
            RunCreationErrorCode.DELAY_EVENT_NOT_FOUND,
            f"지연 이벤트가 없습니다: {delay_event_id}",
        )

    if trigger_type == TriggerType.AUTO:
        snapshot_shipment_id = await repo.find_snapshot_shipment_id(
            company_id, trigger_shipment_schedule_snapshot_id
        )
        if snapshot_shipment_id != event.shipment_id:
            raise AnalysisRunCreationError(
                RunCreationErrorCode.SNAPSHOT_NOT_OF_SHIPMENT,
                "트리거 스냅샷이 지연 선적의 일정 스냅샷이 아닙니다",
            )
        existing = await repo.find_auto_run(
            delay_event_id, trigger_shipment_schedule_snapshot_id
        )
        if existing is not None:
            return AnalysisRunCreation(run=existing, created=False, previous_run=None)

    max_version = await repo.find_max_version_no(delay_event_id)
    is_first = max_version is None
    if is_first != (reason == TriggerReason.EPISODE_STARTED):
        raise AnalysisRunCreationError(
            RunCreationErrorCode.EPISODE_START_MISMATCH,
            "첫 실행은 EPISODE_STARTED 로만, 그 뒤 실행은 다른 사유로만 만듭니다 (ck18)"
            f" — 지금 실행 {0 if is_first else max_version}건, 사유 {reason}",
        )

    previous = None
    if not is_first:
        parent_id = await repo.find_parent_run_id(delay_event_id)
        previous = await lock_analysis_run(db, parent_id)

    if started_at is None:
        started_at = await repo.now()
    else:
        last_input = await repo.find_last_applied_sync_started_at(company_id)
        if last_input is not None and started_at < last_input:
            raise AnalysisRunCreationError(
                RunCreationErrorCode.RUN_TIME_BEFORE_INPUT,
                f"실행 시각 {started_at.isoformat()} 이 고객사 마지막 적재 수집 시작"
                f" {last_input.isoformat()} 보다 이릅니다 (L1 을 잡은 뒤 시각을 구했는지 확인)",
            )
    basis = basis_date(started_at)
    operating_rule_set_id = await repo.find_operating_rule_set_id(company_id, basis)
    if operating_rule_set_id is None:
        raise AnalysisRunCreationError(
            RunCreationErrorCode.OPERATING_RULE_MISSING,
            f"기준일 {basis} 에 적용 중인 운영 기준이 없습니다",
        )
    run_code = next_run_code(
        basis, await repo.find_latest_run_code_of_day(company_id, day_prefix(basis))
    )
    if run_code is None:
        raise AnalysisRunCreationError(
            RunCreationErrorCode.RUN_CODE_EXHAUSTED,
            f"기준일 {basis} 의 run_code 순번 999 를 모두 썼습니다",
        )
    input_seq = await StaleChangeRepository(db).find_max_applied_seq(company_id)

    if previous is not None and previous.is_latest:
        # 부분 고유 인덱스(analysis_runs_is_latest_uq) 때문에 새 실행을 넣기 전에 먼저 내린다
        previous.is_latest = False
        await db.flush()

    run = AnalysisRun(
        analysis_run_id=uuid.uuid7(),
        company_id=company_id,
        run_code=run_code,
        delay_event_id=delay_event_id,
        version_no=1 if is_first else max_version + 1,
        parent_analysis_run_id=None if previous is None else previous.analysis_run_id,
        operating_rule_set_id=operating_rule_set_id,
        input_seq=input_seq,
        trigger_type=trigger_type,
        trigger_reason=reason,
        trigger_shipment_schedule_snapshot_id=trigger_shipment_schedule_snapshot_id,
        requested_by_user_id=requested_by_user_id,
        request_reason=request_reason,
        requested_at=started_at if trigger_type == TriggerType.REANALYSIS else None,
        status=AnalysisRunStatus.DETECTED,
        is_latest=True,
        calculation_contract_version=CALCULATION_CONTRACT_VERSION,
        allocation_rule_version=ALLOCATION_RULE_VERSION,
        started_at=started_at,
    )
    db.add(run)
    await db.flush()
    return AnalysisRunCreation(run=run, created=True, previous_run=previous)


async def record_input_data_versions(
    db: AsyncSession, run: AnalysisRun
) -> list[uuid.UUID]:
    """
    입력 확정 때 읽은 수집 ID 를 analysis_run_data_versions 에 기록하고, 기록된 수집 ID 를 돌려준다.
      - 규칙: 수집 도메인마다 적용 순번이 실행의 입력 순번(input_seq) 이하인 가장 최근 수집 1건.
        시각이 아니라 순번으로 자른다(serviceflow 5.2, STALE 판정과 같은 기준). 엔진 snapshot.build 는 같은 뜻을
        시작 시각으로 고르며, overlay 의 검증을 통과한 실행 20건(도메인 5개, 100행)은 두 규칙 모두 저장값과 같다.
      - 이미 기록된 수집 ID 는 다시 넣지 않는다(analysis_run_data_versions_uk2). 여러 번 불러도 결과가 같다.
    """
    repo = RunCreationRepository(db)
    wanted = await repo.find_domain_latest_sync_run_ids(run.company_id, run.input_seq)
    recorded = await repo.find_recorded_sync_run_ids(run.analysis_run_id)
    for data_sync_run_id in wanted:
        if data_sync_run_id not in recorded:
            db.add(
                AnalysisRunDataVersion(
                    analysis_run_data_version_id=uuid.uuid7(),
                    company_id=run.company_id,
                    analysis_run_id=run.analysis_run_id,
                    data_sync_run_id=data_sync_run_id,
                )
            )
    await db.flush()
    return sorted(set(wanted) | recorded)
