from dataclasses import dataclass
from datetime import date, timedelta

from app.modules.decision.application.draft_policy import (
    EXPLANATION_GENERATION_FAILED,
)
from app.modules.decision.application.exception.decision_exceptions import (
    DecisionErrorCode,
)
from app.modules.delay_analysis.infrastructure.analysis_enums import (
    AnalysisRunStatus,
    FeasibilityStatus,
    OptionType,
)

"""
    최종 선택 허용 판정 (API 19 POST /analysis-runs/{runId}/final-decision, API 명세 v6.2 4.6절 재확인 2~7)
      - 엔진 recheck.selection_check 와 같은 순서·식입니다: 에피소드 종료 → 설명 생성 실패 → STALE·FINALIZED·현재 실행·선택 대기
        → 운영 기준 버전 → 대응안 FEASIBLE → 선택 시점 시간 확인(TIME_ELAPSED).
      - 엔진의 NOT_READY 는 API 18 과 같게 NOT_CURRENT_RUN·RUN_NOT_READY 로 나눕니다.
      - 엔진은 대응안이 없으면(다른 실행의 대응안) OPTION_NOT_FEASIBLE 이지만, API 18 과 맞춰 OPTION_NOT_IN_RUN 으로 나눕니다(11/05 결정).
      - 운영 기준이 바뀌면 OPERATING_RULE_CHANGED(D22 제안 신설), 시간 경과는 코드 ANALYSIS_STALE(사유 TIME_ELAPSED)이고
        이때만 time_elapsed=True 입니다(서비스가 실행을 STALE 로 먼저 기록).
      - 1(요청자 역할)은 라우터가, 없는 실행 404 는 서비스가 먼저 거릅니다. 판정은 DB 를 읽지 않는 순수 함수입니다.
"""


@dataclass(frozen=True)
class LeadTimes:
    """실행의 운영 기준 리드타임(일). 엔진 snapshot.build 의 lt 와 같은 이름"""

    oh_kr: int  # 한국 창고 출고 처리
    air_kr: int  # 한국 → 도착 권역 항공
    inbound_dest: int  # 도착 창고 입고 처리
    oh_donor: int  # 공여 창고 출고 처리
    cd_cross: int  # 공여 창고 → 도착 권역 고객 배송
    oh_dest: int  # 도착 창고 출고 처리
    cd_same: int  # 도착 창고 → 같은 권역 고객 배송


@dataclass(frozen=True)
class SelectionOption:
    option_type: str
    feasibility_status: str
    # 이 대응안으로 회복되는 주문(option_order_outcomes RECOVERED)의 약속 배송일(UTC 날짜)
    recovered_promised_dates: tuple[date, ...]


@dataclass(frozen=True)
class SelectionTarget:
    status: str
    is_latest: bool
    failure_code: str | None
    episode_open: bool
    operating_rule_set_id: object
    active_operating_rule_set_id: object  # 선택일에 적용 중인 운영 기준
    option: SelectionOption | None  # None 이면 같은 실행의 대응안이 아님
    lead_times: LeadTimes
    latest_expected_3pl_inbound_date: date  # 선적의 현재 예상 3PL 입고일(최신 E)
    planned_kr_release_date: date
    selection_day: date


@dataclass(frozen=True)
class SelectionRejection:
    code: DecisionErrorCode
    time_elapsed: bool = False


def _in_time(target: SelectionTarget) -> bool:
    """엔진 selection_check 의 시간 확인 식 그대로"""
    lt, t, option = target.lead_times, target.selection_day, target.option
    promised = option.recovered_promised_dates
    kind = option.option_type
    air = timedelta(lt.oh_kr + lt.air_kr + lt.inbound_dest)
    ok = True
    if kind == OptionType.KR_AIR_REPLENISH:
        ok = t + air < target.latest_expected_3pl_inbound_date
    elif kind == OptionType.PRE_DEPARTURE_AIR:
        ok = (
            max(t, target.planned_kr_release_date) + air
            < target.latest_expected_3pl_inbound_date
        )
    elif kind == OptionType.OTHER_REGION_FULFILL:
        ok = all(t + timedelta(lt.oh_donor + lt.cd_cross) <= p for p in promised)
    if ok and kind != OptionType.KEEP_PLAN:
        ok = all(t <= p - timedelta(lt.oh_dest + lt.cd_same) for p in promised)
    return ok


def selection_rejection(target: SelectionTarget) -> SelectionRejection | None:
    """최종 선택을 거부할 사유. 허용이면 None."""
    if not target.episode_open:
        return SelectionRejection(DecisionErrorCode.EPISODE_CLOSED)
    if (
        target.status == AnalysisRunStatus.FAILED
        and target.failure_code == EXPLANATION_GENERATION_FAILED
    ):
        return SelectionRejection(DecisionErrorCode.EXPLANATION_NOT_READY)
    if target.status == AnalysisRunStatus.STALE:
        return SelectionRejection(DecisionErrorCode.ANALYSIS_STALE)
    if target.status == AnalysisRunStatus.FINALIZED:
        return SelectionRejection(DecisionErrorCode.FINAL_DECISION_EXISTS)
    if not target.is_latest:
        return SelectionRejection(DecisionErrorCode.NOT_CURRENT_RUN)
    if target.status != AnalysisRunStatus.READY_FOR_REVIEW:
        return SelectionRejection(DecisionErrorCode.RUN_NOT_READY)
    if target.operating_rule_set_id != target.active_operating_rule_set_id:
        return SelectionRejection(DecisionErrorCode.OPERATING_RULE_CHANGED)
    if target.option is None:
        return SelectionRejection(DecisionErrorCode.OPTION_NOT_IN_RUN)
    if target.option.feasibility_status != FeasibilityStatus.FEASIBLE:
        return SelectionRejection(DecisionErrorCode.OPTION_NOT_FEASIBLE)
    if not _in_time(target):
        return SelectionRejection(
            DecisionErrorCode.ANALYSIS_STALE_TIME_ELAPSED, time_elapsed=True
        )
    return None
