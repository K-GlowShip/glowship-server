import asyncio
import copy
import hashlib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rls_context import set_rls_context
from app.modules.delay_analysis.infrastructure.engine.engine_input_store import (
    check_input_seq,
    engine_timestamp,
)
from app.modules.delay_analysis.infrastructure.engine.engine_loader import (
    load_engine_module,
    load_verified_engine_dir,
)
from app.modules.delay_analysis.infrastructure.engine.engine_operating_rules import (
    UNSUPPORTED_RULE_HOLD_CODE,
    unsupported_operating_rules,
)
from app.modules.delay_analysis.infrastructure.engine.engine_store_cache import (
    get_company_engine_store,
)

"""
    실행 하나의 엔진 입력 (GLOW-125 2부, docs/analysis/engine-input.md)
      - 엔진 하네스(run_s1_s5.analyze_run)와 같은 순서로 쓴다.
          1) judge_no_delay(session, run)   E ≤ P 면 NO_DELAY. 저장소를 읽기 전(가벼운 조회 두 개)
          2) build_engine_input(session, run)   저장소(#36, 고객사·입력 순번 캐시 #60) → snapshot.build → 운영 기준 검사 → pipeline.analyze.
             엔진 Hold 나 계산이 지원하지 않는 운영 기준(GLOW-100, engine_operating_rules)이면 EngineHold
          3) 도구 T1~T8 은 EngineInput.snap·analysis 에서 옮기기만 한다. decision_of 로 판정 서명·의존 범위를 꺼낸다
      - 엔진 계산(pipeline.analyze)은 실행마다 한 번, 만들 때 스레드에서 한다.
        처음 읽을 때 계산하면(동기 속성) 이벤트 루프가 그동안 멈추므로 미리 계산한다(사용자 결정 11-06).
        보류가 아닌 실행은 어차피 T6·T7·판정 서명에 계산 결과가 필요하다.
      - 엔진 입력 값(입력 확정 시각·단계·예상일)은 #36 정답 대조와 같은 규칙으로 실행 행에서 정한다.
          · 입력 확정 시각 confirm_t = engine_timestamp(run.started_at), 실행 날짜 = confirm_t 의 UTC 날짜(run_code 날짜)
          · 기준일 D = resolve_base_date: min(실행 날짜, 최신 재고일 + 1, 성공 ORDER 수집이 다룬 마지막 접수일 + 1)
            (이슈 #51 사용자 결정). 그날 재고·주문 자료가 아직 입력(입력 순번)에 없는 실행도 마지막 자료 날 기준으로 계산하고, 늦은 원천을 남긴다
          · 단계·예상 입고일 = 실행의 트리거 일정 스냅샷. 트리거가 없으면(재분석) 엔진처럼 confirm_t 까지의 최신 관측
      - 데이터 경고(dataWarnings, 이슈 #122): 엔진 입력을 만들 때(보류여도) 원천별로 정해 EngineInput·EngineHold 에 싣는다.
          · 수집 실패: 원천의 "마지막으로 끝난 수집"(RUNNING 아님, 시작 ≤ confirm_t, 적용 순번이 없거나 입력 순번 이하 —
            엔진 저장소가 수집 이력을 읽는 범위와 같음)이 FAILED
          · 기준일 D 를 당김: resolve_base_date 의 late_sources(재고·주문)
          · 저장은 결과 저장과 같은 트랜잭션에서 한다(run_start/outcome_save.py). 여기서는 값만 만든다.
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
    """입력 검증 보류: 엔진 snapshot.Hold 를 서버 값으로 바꾼 것, 또는 계산이 지원하지 않는 운영 기준.

    실행 ON_HOLD(VALIDATING) → hold_summary(code, message)"""

    code: str  # REQUIRED_DATA_INVALID | MISSING_REQUIRED_DATA
    message: str
    # 보류여도 기준일 D 는 snapshot.build 전에 구한다. 데이터 경고를 정하는 데 쓴다
    base_date_resolution: BaseDateResolution | None = None
    # 데이터 경고(이슈 #122, data_warnings_of). 보류 원인이 수집 문제인 경우가 많아 보류 실행에도 남긴다
    data_warnings: tuple[dict[str, Any], ...] = ()


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
class BaseDateResolution:
    """기준일 D 와 그 근거 (이슈 #51 사용자 결정). D = min(실행 날짜, 재고 한도, ORDER 한도).

    한도는 그 원천 자료로 엔진이 계산할 수 있는 가장 늦은 D 다(snapshot.build 가 D−1 재고 행과
    D−28~D−1 ORDER 수집 성공을 요구한다). 자료가 하나도 없는 원천은 한도 없음(None)으로 두고 엔진 보류에 맡긴다."""

    base_date: date  # D
    run_date: date  # 실행 날짜 = confirm_t 의 UTC 날짜(run_code 날짜와 같음)
    # confirm_t 까지 적재된 최신 재고일 + 1
    inventory_limit: date | None
    # confirm_t 까지 시작한 성공 ORDER 수집이 다룬 마지막 접수일 + 1
    order_limit: date | None
    # 한도가 실행 날짜보다 이른 원천("INVENTORY"·"ORDER", 이름순). dataWarnings 용
    late_sources: tuple[str, ...]


@dataclass(frozen=True)
class EngineInput:
    """실행 하나의 엔진 입력. 도구·설명은 snap·analysis 를 읽기만 한다(고치지 않는다)."""

    analysis_run_id: uuid.UUID
    company_id: uuid.UUID
    delay_event_id: uuid.UUID
    shipment_id: uuid.UUID
    base_date: date  # D (= base_date_resolution.base_date = snap["D"])
    base_date_resolution: BaseDateResolution
    input_seq: int
    confirm_t: str  # 엔진 시각 'YYYY-MM-DDTHH:MM:SSZ'
    snap: dict[str, Any]  # snapshot.build 결과
    analysis: dict[str, Any]  # pipeline.analyze(snap) 결과
    store: Any  # 엔진 Store(한 고객사·한 입력 순번). 스냅샷에 없는 원값(28일 수요 합계 등)을 읽을 때만
    # 데이터 경고(이슈 #122, data_warnings_of). 그래프 상태를 거쳐 결과와 함께 저장한다
    data_warnings: tuple[dict[str, Any], ...] = ()


# 데이터 경고 이유(저장값 reasons, 응답에는 내지 않음 — 화면은 last_status 로 실패·지연을 가름)
LAST_SYNC_FAILED = "LAST_SYNC_FAILED"  # 원천의 마지막으로 끝난 수집이 FAILED
BASE_DATE_LIMITED = (
    "BASE_DATE_LIMITED"  # 그 원천 자료가 실행 날짜까지 없어 기준일 D 를 당김
)


@dataclass(frozen=True)
class LastFinishedSync:
    """원천 하나의 마지막으로 끝난 수집(입력 확정 시각·입력 순번 범위 안)."""

    status: str  # SUCCEEDED | PARTIAL_SUCCESS | FAILED
    finished_at: datetime | None


def data_warnings_of(
    last_syncs: Mapping[str, LastFinishedSync],
    basis: BaseDateResolution | None,
) -> tuple[dict[str, Any], ...]:
    """
    데이터 경고(이슈 #122). 저장소·DB 를 읽지 않는 순수 함수. 원천 이름순, 원천 하나에 항목 하나(이유는 여럿일 수 있음).
      - 수집 실패: 마지막으로 끝난 수집이 FAILED 인 원천(다섯 원천 모두)
      - 기준일 D 를 당김: basis.late_sources(재고·주문). 한도 날짜를 함께 남긴다
      - 수집 이력이 없는 원천은 항목을 만들지 않는다(last_status 에 넣을 값이 없고, 자료가 없으면 엔진이 보류로 알림)
    항목은 JSON 그대로 저장할 수 있는 dict 다(시각은 ISO 문자열).
    """
    late = set(basis.late_sources) if basis is not None else set()
    limits = (
        {"INVENTORY": basis.inventory_limit, "ORDER": basis.order_limit}
        if basis is not None
        else {}
    )
    warnings: list[dict[str, Any]] = []
    for domain in sorted(set(last_syncs) | late):
        last = last_syncs.get(domain)
        if last is None:
            continue
        reasons = []
        if last.status == "FAILED":
            reasons.append(LAST_SYNC_FAILED)
        if domain in late:
            reasons.append(BASE_DATE_LIMITED)
        if not reasons:
            continue
        limit = limits.get(domain) if domain in late else None
        warnings.append(
            {
                "source_domain": domain,
                "last_status": last.status,
                "last_finished_at": (
                    last.finished_at.isoformat()
                    if last.finished_at is not None
                    else None
                ),
                "reasons": reasons,
                "limit_date": limit.isoformat() if limit is not None else None,
            }
        )
    return tuple(warnings)


# 원천별 마지막으로 끝난 수집. 엔진 저장소가 수집 이력을 읽는 범위(engine_input_store: 적용 순번이 없거나 입력 순번 이하)와
# 엔진 store.runs_until(시작 ≤ confirm_t, 초 아래 버림)을 그대로 따른다. 실패 수집은 적용 순번이 없어 입력 순번이 바뀌지 않으므로
# (고객사, 입력 순번)으로 캐시된 저장소에는 안 보일 수 있어, 저장소가 아니라 이 질의로 읽는다
_LAST_FINISHED_SYNCS_SQL = text(
    "SELECT DISTINCT ON (source_domain) source_domain, status, finished_at"
    " FROM data_sync_runs"
    " WHERE company_id = :company_id AND status <> 'RUNNING'"
    "   AND date_trunc('second', started_at) <= :confirm_at"
    "   AND (applied_seq IS NULL OR applied_seq <= :input_seq)"
    " ORDER BY source_domain, started_at DESC, data_sync_run_id DESC"
)


async def _last_finished_syncs(
    session: AsyncSession, run: AnalysisRunLike
) -> dict[str, LastFinishedSync]:
    rows = (
        await session.execute(
            _LAST_FINISHED_SYNCS_SQL,
            {
                "company_id": run.company_id,
                "confirm_at": run.started_at.replace(microsecond=0),  # = confirm_t
                "input_seq": run.input_seq,
            },
        )
    ).all()
    return {
        row.source_domain: LastFinishedSync(row.status, row.finished_at) for row in rows
    }


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
    # confirm_t 의 UTC 날짜. D 는 저장소를 읽은 뒤 resolve_base_date 가 정한다
    run_date: date
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
            f"실행 {run.analysis_run_id} 의 지연 이벤트·선적을 고객사 {run.company_id} 범위에서 찾지 못했습니다."
            " 워커 역할 세션인지, 고객사가 ACTIVE 인지 확인하세요"
        )
    if run.trigger_shipment_schedule_snapshot_id is not None and row.stage is None:
        raise LookupError(
            f"실행 {run.analysis_run_id} 의 트리거 일정 스냅샷 {run.trigger_shipment_schedule_snapshot_id} 이 없습니다"
        )
    confirm_t = engine_timestamp(run.started_at)
    return _RunInput(
        shipment_id=row.shipment_id,
        confirm_t=confirm_t,
        run_date=date.fromisoformat(confirm_t[:10]),
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


# 엔진 store.order_collection_ok 와 같은 규칙
ORDER_OK_STATUSES = ("SUCCEEDED", "PARTIAL_SUCCESS")
# 이 시각 전에 시작한 수집 = 2025 초기 적재(2025 접수일 전부를 다룸).
# 엔진도 같은 값을 고정한다: store.order_collection_ok 의 "2026-01-01"·"2025-12-31", store.DCR_SQL 의 '2026-01-01'.
# D 는 엔진 판정과 같아야 하므로 데이터에서 따로 정하지 않는다. 엔진이 바꾸면 해시 목록과 Store 소스 대조 시험이 멈춘다(#67 리뷰)
INITIAL_LOAD_BEFORE = "2026-01-01"
_INITIAL_LOAD_LIMIT = date(2026, 1, 1)  # 2025-12-31 + 1


def resolve_base_date(store, company_id: str, confirm_t: str) -> BaseDateResolution:
    """기준일 D (이슈 #51). 저장소를 읽기만 하는 순수 함수. company_id 는 엔진 저장소 열쇠(문자열 UUID).

    저장소는 이미 실행의 입력 순번까지 거른 것이라(#36) 입력 순번 기준이 저절로 지켜진다.
      · 재고 한도: 엔진 inventory_row 처럼 그 행을 만든 수집의 started_at ≤ confirm_t 인 행만 센다(고객사 전체 최대 재고일)
      · ORDER 한도: 엔진 order_collection_ok 처럼 성공 ORDER 수집만, h+1 에 시작한 수집이 h 를 다룬다
        → 다룬 마지막 접수일 + 1 = 시작 날짜. 2026-01-01 전에 시작한 초기 적재는 2025-12-31 까지 다뤄 2026-01-01
    """
    run_date = date.fromisoformat(confirm_t[:10])
    runs = store.runs
    inventory_days = [
        row["inventory_date"]
        for row in store.inv.values()
        if row["company_id"] == company_id
        and runs[row["data_sync_run_id"]]["started_at"] <= confirm_t
    ]
    inventory_limit = (
        date.fromisoformat(max(inventory_days)) + timedelta(days=1)
        if inventory_days
        else None
    )
    order_limits = [
        _INITIAL_LOAD_LIMIT
        if r["started_at"] < INITIAL_LOAD_BEFORE
        else date.fromisoformat(r["started_at"][:10])
        for r in store.runs_until(company_id, confirm_t)
        if r["source_domain"] == "ORDER" and r["status"] in ORDER_OK_STATUSES
    ]
    order_limit = max(order_limits) if order_limits else None
    limits = {"INVENTORY": inventory_limit, "ORDER": order_limit}
    return BaseDateResolution(
        base_date=min([run_date, *(v for v in limits.values() if v is not None)]),
        run_date=run_date,
        inventory_limit=inventory_limit,
        order_limit=order_limit,
        late_sources=tuple(
            sorted(k for k, v in limits.items() if v is not None and v < run_date)
        ),
    )


def _build_and_analyze(
    store, company_id: str, ri: _RunInput
) -> tuple[dict, dict, BaseDateResolution] | EngineHold:
    snapshot = load_engine_module("snapshot")
    basis = resolve_base_date(store, company_id, ri.confirm_t)
    try:
        snap = snapshot.build(
            store,
            _calendar(),
            str(ri.shipment_id),
            basis.base_date,
            ri.confirm_t,
            stage=ri.stage,
            E=ri.expected.isoformat() if ri.expected is not None else None,
        )
    except snapshot.Hold as hold:
        return EngineHold(
            code=hold.code, message=hold.message, base_date_resolution=basis
        )
    # 엔진이 고른 운영 기준 버전·리드타임 행을 본다. 엔진 보류가 먼저이고, 걸리면 계산하지 않는다
    unsupported = unsupported_operating_rules(store, snap)
    if unsupported is not None:
        return EngineHold(
            code=UNSUPPORTED_RULE_HOLD_CODE,
            message=unsupported,
            base_date_resolution=basis,
        )
    return snap, load_engine_module("pipeline").analyze(snap), basis


async def build_engine_input(
    session: AsyncSession, run: AnalysisRunLike
) -> EngineInput | EngineHold:
    """실행 하나의 엔진 입력. 입력 검증 실패(엔진 Hold, 계산이 지원하지 않는 운영 기준)는 EngineHold 로 돌려준다(예외 아님).

    저장소·입력 순번이 맞지 않는 등 시스템 문제는 EngineInputError·LookupError 로 올린다(보류가 아님)."""
    ri = await _run_input(session, run)
    store = await get_company_engine_store(session, run.company_id, run.input_seq)
    check_input_seq(store, run.company_id, ri.confirm_t, run.input_seq)
    built = await asyncio.to_thread(_build_and_analyze, store, str(run.company_id), ri)
    last_syncs = await _last_finished_syncs(session, run)
    if isinstance(built, EngineHold):
        return replace(
            built,
            data_warnings=data_warnings_of(last_syncs, built.base_date_resolution),
        )
    snap, analysis, basis = built
    return EngineInput(
        analysis_run_id=run.analysis_run_id,
        company_id=run.company_id,
        delay_event_id=run.delay_event_id,
        shipment_id=ri.shipment_id,
        base_date=basis.base_date,
        base_date_resolution=basis,
        input_seq=run.input_seq,
        confirm_t=ri.confirm_t,
        snap=snap,
        analysis=analysis,
        store=store,
        data_warnings=data_warnings_of(last_syncs, basis),
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
