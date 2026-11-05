import asyncio
import copy
import hashlib
import uuid
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rls_context import set_rls_context
from app.modules.delay_analysis.infrastructure.engine.engine_input_store import (
    check_input_seq,
    engine_timestamp,
    load_company_engine_store,
)
from app.modules.delay_analysis.infrastructure.engine.engine_loader import (
    load_engine_module,
    load_verified_engine_dir,
)

"""
    실행 하나의 엔진 입력 (GLOW-125 2부, docs/analysis/engine-input.md)
      - 엔진 하네스(run_s1_s5.analyze_run)와 같은 순서로 쓴다.
          1) judge_no_delay(session, run)   E ≤ P 면 NO_DELAY. 저장소를 읽기 전(가벼운 조회 두 개)
          2) build_engine_input(session, run)   저장소(#36) → snapshot.build → pipeline.analyze. Hold 면 EngineHold
          3) 도구 T1~T8 은 EngineInput.snap·analysis 에서 옮기기만 한다. decision_of 로 판정 서명·의존 범위를 꺼낸다
      - 엔진 계산(pipeline.analyze)은 실행마다 한 번, 만들 때 스레드에서 한다.
        처음 읽을 때 계산하면(동기 속성) 이벤트 루프가 그동안 멈추므로 미리 계산한다(사용자 결정 11-06).
        보류가 아닌 실행은 어차피 T6·T7·판정 서명에 계산 결과가 필요하다.
      - 엔진 입력 값(D·입력 확정 시각·단계·예상일)은 #36 정답 대조와 같은 규칙으로 실행 행에서 정한다.
          · 입력 확정 시각 confirm_t = engine_timestamp(run.started_at), D = confirm_t 의 UTC 날짜
          · 단계·예상 입고일 = 실행의 트리거 일정 스냅샷. 트리거가 없으면(재분석) 엔진처럼 confirm_t 까지의 최신 관측
      - 읽기 전용이다. 커밋하지 않는다.
"""

# 행사 일정 설정(엔진 하네스 run_s1_s5.CAL 과 같은 파일). 파일 해시는 엔진 매니페스트(engine_manifest.json)가 고정한다.
CALENDAR_FILE = ("glowship_engine", "config", "event_calendar_2026h1_v1.json")


class AnalysisRunLike(Protocol):
    """build_engine_input·judge_no_delay 가 읽는 실행 값 (analysis_runs 행, ORM AnalysisRun 이 그대로 맞음)"""

    analysis_run_id: uuid.UUID
    company_id: uuid.UUID
    delay_event_id: uuid.UUID
    input_seq: int
    started_at: Any  # datetime(시간대 있음)
    trigger_shipment_schedule_snapshot_id: uuid.UUID | None


@dataclass(frozen=True)
class EngineHold:
    """엔진 snapshot.Hold 를 서버 값으로 바꾼 것. 실행 ON_HOLD(VALIDATING) → hold_summary(code, message)"""

    code: str  # REQUIRED_DATA_INVALID | MISSING_REQUIRED_DATA
    message: str


@dataclass(frozen=True)
class NoDelay:
    """NO_DELAY 판정(엔진 pipeline.no_delay). save_no_delay 에 판정 서명·SHA-256 을 넘긴다."""

    decision_signature: dict[str, Any]
    decision_signature_sha256: str


@dataclass(frozen=True)
class EngineDecision:
    """판정 서명·의존 범위 (엔진 pipeline.analyze 결과). 결과 저장의 같은 이름 칸에 그대로 넣는다."""

    status: str  # READY_FOR_REVIEW | NO_RISK
    result_class: str | None
    dependency_scope: dict[str, Any] | None
    decision_signature: dict[str, Any]
    decision_signature_sha256: str


@dataclass(frozen=True)
class EngineInput:
    """실행 하나의 엔진 입력. 도구·설명은 snap·analysis 를 읽기만 한다(고치지 않는다)."""

    analysis_run_id: uuid.UUID
    company_id: uuid.UUID
    delay_event_id: uuid.UUID
    shipment_id: uuid.UUID
    base_date: date  # D
    input_seq: int
    confirm_t: str  # 엔진 시각 'YYYY-MM-DDTHH:MM:SSZ'
    snap: dict[str, Any]  # snapshot.build 결과
    analysis: dict[str, Any]  # pipeline.analyze(snap) 결과
    store: Any  # 엔진 Store(한 고객사·한 입력 순번). 스냅샷에 없는 원값(28일 수요 합계 등)을 읽을 때만


@lru_cache(maxsize=1)
def _calendar():
    """행사 일정 설정. 엔진처럼 읽지 못하면 CalendarInvalid 를 그대로 돌려 snapshot.build 가 보류(MISSING_REQUIRED_DATA)로 바꾸게 한다."""
    calendar_config = load_engine_module("calendar_config")
    path = load_verified_engine_dir().joinpath(*CALENDAR_FILE)
    try:
        raw = path.read_bytes()
        return calendar_config.load_calendar(path, hashlib.sha256(raw).hexdigest())
    except (OSError, calendar_config.CalendarInvalid) as error:
        return calendar_config.CalendarInvalid(str(error))


_RUN_INPUT_SQL = text(
    "SELECT e.shipment_id, s.planned_3pl_inbound_date, sn.stage, sn.expected_3pl_inbound_date"
    " FROM delay_events e"
    " JOIN shipments s ON s.shipment_id = e.shipment_id AND s.company_id = e.company_id"
    " LEFT JOIN shipment_schedule_snapshots sn"
    "   ON sn.shipment_schedule_snapshot_id = :trigger_id AND sn.company_id = e.company_id"
    " WHERE e.delay_event_id = :delay_event_id AND e.company_id = :company_id"
)

# 트리거가 없는 실행(재분석)의 예상 입고일 = 엔진 store.snapshot_at(선적, confirm_t):
#   입력 순번까지 적재된 관측 중 observed_at(초 아래 버림) ≤ confirm_t 인 마지막 것. 같은 초면 읽은 순서(ctid)의 마지막
_LATEST_SNAPSHOT_SQL = text(
    "SELECT sn.stage, sn.expected_3pl_inbound_date FROM shipment_schedule_snapshots sn"
    " WHERE sn.company_id = :company_id AND sn.shipment_id = :shipment_id"
    "   AND sn.data_sync_run_id IN (SELECT data_sync_run_id FROM data_sync_runs"
    "                               WHERE company_id = :company_id AND applied_seq <= :input_seq)"
    "   AND date_trunc('second', sn.observed_at) <= :confirm_at"
    " ORDER BY date_trunc('second', sn.observed_at) DESC, sn.ctid DESC LIMIT 1"
)


@dataclass(frozen=True)
class _RunInput:
    shipment_id: uuid.UUID
    confirm_t: str
    base_date: date
    planned: date  # P
    stage: str | None  # None 이면 엔진이 confirm_t 까지의 최신 관측을 쓴다
    expected: date | None  # E. None 이면 위와 같음


async def _run_input(session: AsyncSession, run: AnalysisRunLike) -> _RunInput:
    await set_rls_context(session, user_id=None, company_id=run.company_id)
    row = (
        await session.execute(
            _RUN_INPUT_SQL,
            {
                "delay_event_id": run.delay_event_id,
                "company_id": run.company_id,
                "trigger_id": run.trigger_shipment_schedule_snapshot_id,
            },
        )
    ).one_or_none()
    if row is None:
        raise LookupError(
            f"실행 {run.analysis_run_id} 의 지연 이벤트·선적을 고객사 {run.company_id} 범위에서 찾지 못했습니다"
        )
    if run.trigger_shipment_schedule_snapshot_id is not None and row.stage is None:
        raise LookupError(
            f"실행 {run.analysis_run_id} 의 트리거 일정 스냅샷 {run.trigger_shipment_schedule_snapshot_id} 이 없습니다"
        )
    confirm_t = engine_timestamp(run.started_at)
    return _RunInput(
        shipment_id=row.shipment_id,
        confirm_t=confirm_t,
        base_date=date.fromisoformat(confirm_t[:10]),
        planned=row.planned_3pl_inbound_date,
        stage=row.stage,
        expected=row.expected_3pl_inbound_date,
    )


async def _expected_for_no_delay(
    session: AsyncSession, run: AnalysisRunLike, ri: _RunInput
) -> date:
    if ri.expected is not None:
        return ri.expected
    latest = (
        await session.execute(
            _LATEST_SNAPSHOT_SQL,
            {
                "company_id": run.company_id,
                "shipment_id": ri.shipment_id,
                "input_seq": run.input_seq,
                "confirm_at": run.started_at.replace(
                    microsecond=0
                ),  # = confirm_t(초 아래 버림)
            },
        )
    ).one_or_none()
    if latest is None:
        raise LookupError(
            f"선적 {ri.shipment_id} 의 일정 관측이 입력 확정 시각 {ri.confirm_t}·입력 순번 {run.input_seq} 까지 없습니다"
        )
    return latest.expected_3pl_inbound_date


async def judge_no_delay(session: AsyncSession, run: AnalysisRunLike) -> NoDelay | None:
    """엔진 하네스 2단계: E ≤ P 면 NO_DELAY(입력 검증·재고 복원보다 먼저, 저장소를 읽지 않음). 아니면 None."""
    ri = await _run_input(session, run)
    expected = await _expected_for_no_delay(session, run, ri)
    result = load_engine_module("pipeline").no_delay(ri.planned, expected)
    if result is None:
        return None
    return NoDelay(
        decision_signature=copy.deepcopy(result["signature"]),
        decision_signature_sha256=result["sha"],
    )


def _build_and_analyze(store, ri: _RunInput) -> tuple[dict, dict] | EngineHold:
    snapshot = load_engine_module("snapshot")
    try:
        snap = snapshot.build(
            store,
            _calendar(),
            str(ri.shipment_id),
            ri.base_date,
            ri.confirm_t,
            stage=ri.stage,
            E=ri.expected.isoformat() if ri.expected is not None else None,
        )
    except snapshot.Hold as hold:
        return EngineHold(code=hold.code, message=hold.message)
    return snap, load_engine_module("pipeline").analyze(snap)


async def build_engine_input(
    session: AsyncSession, run: AnalysisRunLike
) -> EngineInput | EngineHold:
    """실행 하나의 엔진 입력. 입력 검증 실패(엔진 Hold)는 EngineHold 로 돌려준다(예외 아님).

    저장소·입력 순번이 맞지 않는 등 시스템 문제는 EngineInputError·LookupError 로 올린다(보류가 아님)."""
    ri = await _run_input(session, run)
    store = await load_company_engine_store(session, run.company_id, run.input_seq)
    check_input_seq(store, run.company_id, ri.confirm_t, run.input_seq)
    built = await asyncio.to_thread(_build_and_analyze, store, ri)
    if isinstance(built, EngineHold):
        return built
    snap, analysis = built
    return EngineInput(
        analysis_run_id=run.analysis_run_id,
        company_id=run.company_id,
        delay_event_id=run.delay_event_id,
        shipment_id=ri.shipment_id,
        base_date=ri.base_date,
        input_seq=run.input_seq,
        confirm_t=ri.confirm_t,
        snap=snap,
        analysis=analysis,
        store=store,
    )


def decision_of(engine: EngineInput) -> EngineDecision:
    """판정 서명·SHA-256·의존 범위 (사용자 결정 2: 엔진 analysis 의 signature·sha·scope). 복사본을 돌려준다."""
    a = engine.analysis
    return EngineDecision(
        status=a["status"],
        result_class=a["result_class"],
        dependency_scope=copy.deepcopy(a["scope"]),
        decision_signature=copy.deepcopy(a["signature"]),
        decision_signature_sha256=a["sha"],
    )
