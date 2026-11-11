"""
데모 시드: 열린 지연과 선택 대기 실행 (GLOW-89, 명세 v6.2 D12·C28)
  - 확정 DB 는 지연이 모두 닫혀 있어 판단 API 18~20 을 시연할 수 없다. 이 스크립트가 가상 TMS 시나리오로
    7월 초 수동 수집을 한 번 하고(열린 지연), 그 지연마다 분석 실행을 만들어 엔진으로 계산한 결과를 저장한다.
  - 순서
      1. 수동 수집(API 25 와 같은 ManualSyncService): 시나리오(VIRTUAL_TMS_SCENARIO_PATH)의 그날 응답 적재
         → 수집이 지연을 판정해 에피소드를 연다. 시나리오 선적이 이미 있으면 건너뛴다(여러 번 돌려도 같음).
      2. 열린 지연마다 실행: 현재 실행이 없으면 create_analysis_run(AUTO, EPISODE_STARTED, 원인 스냅샷 = 감지 스냅샷,
         실행 시각 = 고객사 마지막 적재 수집 시작, 이슈 #50). 진행 중(DETECTED 등) 실행이 있으면 그것을 쓴다.
         이미 결과가 있는 실행은 건너뛴다.
      3. 엔진 계산(에이전트·LLM 없이 결과가 매번 같음): PR #36 엔진 입력 저장소 → check_input_seq →
         snapshot.build(D = 고객사 최신 재고일 + 1) → pipeline.analyze → 설명(서버 generate_explanation, PR #45: 엔진 생성기·
         결정적 mock 모델, 검증 통과만).
         D 를 실행 날짜로 두면 SHIPMENT 만 나아간 데모에서 엔진이 "ORDER 수집 결측일"로 보류한다(11/06 시험).
      4. 저장: 결과 표 4개 행은 엔진 storage.rows_for 로 만든다(확정 DB overlay 를 만든 함수, 행 모양이 같음).
         서버 결과 저장(save_analysis_result)은 에이전트 도구 출력(T6·T7·T10·T11)을 받는데, 엔진 결과를 그 모양으로
         바꾸는 코드가 아직 없어(에이전트 도구 범위) 시드는 이 경로를 쓴다(설계 GLOW-89 7절 "임시 경로").
         실행 값(상태·결과 분류·의존 범위·판정 서명·설명)과 완료 시각(실행 시작 + 1초, 결과 저장과 같은 규칙)을 쓴다.
  - 엔진 원본은 고치지 않는다(PR #25 해시 대조). 보류(Hold)·설명 실패면 멈추고 알린다(데모에 쓸 수 없음).
  - 되돌리기: 공유 묶음 00_필수_최신DB_전체재적재 로 다시 적재한다(수집·변경 기록·적용 순번까지 되돌려야 해서).

사용법 (alembic upgrade head, 확정 DB 적재 뒤):
  VIRTUAL_TMS_SCENARIO_PATH=<시나리오 JSON>  GLOWSHIP_ENGINE_DIR=<엔진 폴더>  (VIRTUAL_CLOCK_START 기본값)
  uv run python -m scripts.seed_demo_runs [--company A_BEAUTY] [--dry-run]
    --dry-run : 같은 일을 한 뒤 모두 되돌린다(DB 그대로). 결과만 출력
"""

import argparse
import asyncio
import hashlib
import json
import sys
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import UUID as SA_UUID
from sqlalchemy import Date, DateTime, Numeric, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import get_settings
from app.core.locks import lock_analysis_run, lock_company
from app.core.model_registry import Base
from app.core.security.tenant import TenantContext
from app.modules.data_sync.application.ingest.ingest_service import DataIngestService
from app.modules.data_sync.application.ingest.source_payload import (
    SourceInventory,
    SourceOrder,
    SourceOrderLine,
    SourceRecord,
)
from app.modules.data_sync.application.manual_sync_service import ManualSyncService
from app.modules.data_sync.application.virtual_clock import (
    SOURCE_SLOT,
    VIRTUAL_FINISH_AFTER,
)
from app.modules.data_sync.infrastructure.data_sync_enums import (
    ConnectionMethod,
    DataSyncRunStatus,
    SourceDomain,
)
from app.modules.data_sync.infrastructure.repository.ingest_repository import (
    IngestRepository,
)
from app.modules.data_sync.infrastructure.virtual_tms import VirtualTmsScenario
from app.modules.delay_analysis.application.agent.explanation import (
    ExplanationPassed,
    generate_explanation,
)
from app.modules.delay_analysis.application.run_creation.run_creation_service import (
    create_analysis_run,
    record_input_data_versions,
)
from app.modules.delay_analysis.application.stale.stale_service import (
    StaleJudgmentService,
)
from app.modules.delay_analysis.infrastructure.analysis_enums import (
    AnalysisRunStatus,
    TriggerReason,
)
from app.modules.delay_analysis.infrastructure.analysis_run import AnalysisRun
from app.modules.delay_analysis.infrastructure.engine import engine_input_store as EIS
from app.modules.delay_analysis.infrastructure.engine.engine_loader import (
    load_engine_module,
    load_verified_engine_dir,
)
from app.modules.delay_analysis.infrastructure.repository.run_creation_repository import (
    RunCreationRepository,
)
from app.modules.delay_analysis.infrastructure.repository.stale_change_repository import (
    StaleChangeRepository,
)

# 결과 표 (엔진 storage.rows_for 가 만드는 표 중 결과 행만 넣는다. 실행 행은 서버가 만든 것을 갱신)
RESULT_TABLES = (
    "inventory_impact_results",
    "at_risk_orders",
    "response_options",
    "option_order_outcomes",
)
IN_PROGRESS = {
    AnalysisRunStatus.DETECTED,
    AnalysisRunStatus.VALIDATING,
    AnalysisRunStatus.ANALYZING_IMPACT,
    AnalysisRunStatus.ANALYZING_OPTIONS,
    AnalysisRunStatus.VALIDATING_OUTPUT,
}
# NO_RISK 의 status_reason (엔진 하네스 run_s1_s5.py analyze_run)
NO_RISK_REASON = {
    "WARNING_NO_RISK": "NO_DELAY_INCREMENT; PRE_EXISTING_SHORTAGE",
    "PURE_NO_RISK": "NO_DELAY_INCREMENT; NONE",
}

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class DemoSeedError(Exception):
    """데모에 쓸 수 없는 결과(보류·설명 실패·설정 없음). 아무것도 커밋하지 않는다."""


@dataclass(frozen=True)
class SeededRun:
    shipment_code: str
    stage: str
    run_code: str
    status: str
    result_class: str | None
    options: tuple[tuple[str, str, str | None, bool], ...]  # (유형, 판정, 사유, 추천)


_OPEN_EVENTS = text(
    """
    SELECT e.delay_event_id, e.shipment_id, e.stage, e.expected_3pl_inbound_date,
           e.detected_shipment_schedule_snapshot_id, s.shipment_code
      FROM delay_events e JOIN shipments s ON s.shipment_id = e.shipment_id
     WHERE e.company_id = :c AND e.closed_at IS NULL
     ORDER BY s.shipment_code, e.episode_no
    """
)
_LATEST_INVENTORY_DATE = text(
    "SELECT max(inventory_date) FROM inventory_daily WHERE company_id = :c"
)


# --- 1. 수동 수집 -------------------------------------------------------------------------------


async def sync_scenario(
    db: AsyncSession,
    open_session: SessionFactory,
    company_id: uuid.UUID,
    company_code: str,
    scenario: VirtualTmsScenario,
    virtual_start: datetime,
) -> bool:
    """시나리오 선적이 아직 없으면 수동 수집을 한 번 한다. 수집했으면 True."""
    codes = {
        s.get("shipmentNo")
        for r in scenario.responses
        if r.company_code == company_code
        for s in r.data
    }
    existing = await db.scalar(
        text(
            "SELECT count(*) FROM shipments WHERE company_id = :c AND shipment_code = ANY(:codes)"
        ),
        {"c": company_id, "codes": sorted(codes)},
    )
    if existing:
        return False
    service = ManualSyncService(
        open_session=open_session,
        virtual_start=virtual_start,
        load_scenario=lambda: scenario,
    )
    ticket = await service.request(
        TenantContext(user_id=None, company_id=company_id), SourceDomain.SHIPMENT
    )
    result = await service.run(ticket)
    if result is None or result.rejections:
        raise DemoSeedError(
            f"수동 수집이 실패했거나 거부가 있습니다: {None if result is None else result.rejections}"
        )
    return True


# --- 0. 보완 하루(선택) ---------------------------------------------------------------------------

SUPPLEMENT_SOURCES = (
    SourceDomain.PRODUCT,
    SourceDomain.WAREHOUSE,
    SourceDomain.ORDER,
    SourceDomain.SHIPMENT,
    SourceDomain.INVENTORY,
)


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value)  # 'Z' 를 그대로 읽음 (Python 3.11+)


def supplement_records(supplement: dict) -> dict[SourceDomain, list[SourceRecord]]:
    """census extend_day.py 결과(07-01 하루) → 원천별 레코드. 상품·창고·선적은 변경 없음(빈 응답)."""
    orders = [
        SourceOrder(
            order_code=o["order_code"],
            sales_channel=o["sales_channel"],
            customer_region=o["customer_region"],
            warehouse_code=o["warehouse_code"],
            ordered_at=_utc(o["ordered_at"]),
            promised_delivery_at=_utc(o["promised_delivery_at"]),
            status=o["status"],
            lines=tuple(
                SourceOrderLine(
                    line_no=line["line_no"],
                    sku_code=line["sku_code"],
                    ordered_quantity=line["ordered_quantity"],
                    shipped_quantity=line["shipped_quantity"],
                    line_status=line["line_status"],
                )
                for line in o["lines"]
            ),
        )
        for o in supplement["order_response"]
    ]
    inventory = [
        SourceInventory(
            inventory_date=date.fromisoformat(r["inventory_date"]),
            warehouse_code=r["warehouse_code"],
            sku_code=r["sku_code"],
            on_hand_quantity=r["on_hand_quantity"],
            reserved_quantity=r["reserved_quantity"],
            available_quantity=r["available_quantity"],
            outbound_quantity=r["outbound_quantity"],
        )
        for r in supplement["inventory_response"]
    ]
    return {
        SourceDomain.PRODUCT: [],
        SourceDomain.WAREHOUSE: [],
        SourceDomain.ORDER: orders,
        SourceDomain.SHIPMENT: [],
        SourceDomain.INVENTORY: inventory,
    }


async def load_supplement(
    db: AsyncSession, company_id: uuid.UUID, supplement: dict
) -> bool:
    """
    보완 하루(census 가 만든 07-01 주문·재고)를 관측일(07-02) 가상 시각에 다섯 원천 수집으로 적재한다(위험 주문 보완, GLOW-89).
      - 원천 자리(SOURCE_SLOT)·완료 = 시작 + 30초는 수동 수집과 같다. 그래서 뒤의 SHIPMENT 수동 수집은 다음 날(07-03)이 된다.
      - 이미 그날 재고가 있으면 건너뛴다(여러 번 돌려도 같음). 거부가 하나라도 있으면 멈춘다.
    """
    day = date.fromisoformat(supplement["day"])
    latest = await db.scalar(_LATEST_INVENTORY_DATE, {"c": company_id})
    if (
        latest is not None and latest >= day
    ):  # 재고가 하나도 없는 고객사면 None (PR #80 리뷰)
        # 이미 넣었거나 파일 날짜가 맞지 않는 경우. CLI 출력만으로 알 수 있게 남긴다(이슈 #103)
        print(
            f"보완 하루 건너뜀: 최신 재고일 {latest} 이 보완 날짜 {day} 이상"
            "(이미 넣었거나 보완 파일 날짜가 맞지 않음)"
        )
        return False
    observed = datetime.combine(
        date.fromisoformat(supplement["observed_at"]), datetime.min.time(), UTC
    )
    records = supplement_records(supplement)
    stale = StaleJudgmentService(db, StaleChangeRepository(db))
    for domain in SUPPLEMENT_SOURCES:
        at = observed + timedelta(minutes=SOURCE_SLOT[domain])
        service = DataIngestService(
            db,
            IngestRepository(db),
            stale,
            now=lambda at=at: at,
            finish_after=VIRTUAL_FINISH_AFTER,
        )
        result = await service.ingest(
            company_id, domain, ConnectionMethod.API, records[domain]
        )
        if result.rejections or result.status != DataSyncRunStatus.SUCCEEDED:
            raise DemoSeedError(
                f"보완 수집 {domain.value} 이 실패했거나 거부가 있습니다: {result.status} {result.rejections[:3]}"
            )
    return True


# --- 2. 실행 -------------------------------------------------------------------------------------


async def _current_run(
    db: AsyncSession, delay_event_id: uuid.UUID
) -> AnalysisRun | None:
    return await db.scalar(
        select(AnalysisRun).where(
            AnalysisRun.delay_event_id == delay_event_id, AnalysisRun.is_latest
        )
    )


async def ensure_run(
    db: AsyncSession, company_id: uuid.UUID, event
) -> AnalysisRun | None:
    """열린 지연의 진행 중 실행. 없으면 만든다(실행 시각 = 마지막 적재 수집 시작). 이미 결과가 있으면 None."""
    run = await _current_run(db, event.delay_event_id)
    if run is not None:
        return run if run.status in IN_PROGRESS else None
    await lock_company(db, company_id)
    started_at = await RunCreationRepository(db).find_last_applied_sync_started_at(
        company_id
    )
    creation = await create_analysis_run(
        db,
        company_id=company_id,
        delay_event_id=event.delay_event_id,
        trigger_reason=TriggerReason.EPISODE_STARTED,
        trigger_shipment_schedule_snapshot_id=event.detected_shipment_schedule_snapshot_id,
        started_at=started_at,
    )
    return creation.run


# --- 3·4. 엔진 계산과 저장 -----------------------------------------------------------------------


@dataclass(frozen=True)
class Engine:
    snapshot: Any
    pipeline: Any
    storage: Any
    calendar: Any

    @classmethod
    def load(cls) -> Engine:
        path = (
            load_verified_engine_dir()
            / "glowship_engine"
            / "config"
            / "event_calendar_2026h1_v1.json"
        )
        calendar = load_engine_module("calendar_config").load_calendar(
            path, hashlib.sha256(path.read_bytes()).hexdigest()
        )
        return cls(
            snapshot=load_engine_module("snapshot"),
            pipeline=load_engine_module("pipeline"),
            storage=load_engine_module("storage"),
            calendar=calendar,
        )


def _typed(table, row: dict[str, Any]) -> dict[str, Any]:
    """엔진 행(문자열 날짜·Raw 숫자) → DB 열 타입"""
    out = {}
    for key, value in row.items():
        column = table.c[key]
        if value is None or not isinstance(value, str):
            out[key] = value
        elif isinstance(column.type, DateTime):
            out[key] = datetime.fromisoformat(
                value
            )  # 'Z' 를 그대로 읽음 (Python 3.11+)
        elif isinstance(column.type, Date):
            out[key] = date.fromisoformat(value)
        elif isinstance(column.type, Numeric):
            out[key] = Decimal(value)
        elif isinstance(column.type, SA_UUID):
            out[key] = uuid.UUID(value)
        else:
            out[key] = value
    return out


async def compute_and_save(
    db: AsyncSession, engine: Engine, run: AnalysisRun, event, basis_day: date
) -> SeededRun:
    """엔진으로 계산해 결과 행과 실행 값을 저장한다(L2). 보류·설명 실패면 DemoSeedError."""
    company_id = run.company_id
    confirm_t = EIS.engine_timestamp(run.started_at)
    store = await EIS.load_company_engine_store(db, company_id, run.input_seq)
    EIS.check_input_seq(store, company_id, confirm_t, run.input_seq)
    expected = event.expected_3pl_inbound_date.isoformat()
    try:
        snap = engine.snapshot.build(
            store,
            engine.calendar,
            str(event.shipment_id),
            basis_day,
            confirm_t,
            stage=event.stage,
            E=expected,
        )
    except engine.snapshot.Hold as hold:
        raise DemoSeedError(
            f"{event.shipment_code}: 엔진 보류 {hold.code} {hold.message}"
        ) from hold
    result = engine.pipeline.analyze(snap)
    status = result["status"]
    if status not in ("READY_FOR_REVIEW", "NO_RISK"):
        raise DemoSeedError(f"{event.shipment_code}: 데모에 쓸 수 없는 결과 {status}")

    run_id = str(run.analysis_run_id)
    finished_at = run.started_at + timedelta(
        seconds=1
    )  # 결과 저장과 같은 규칙(이슈 #50)
    explanation = None
    if status == "READY_FOR_REVIEW":
        # 서버 설명 생성(PR #45): 엔진 generator·결정적 mock 모델, 해시를 대조한 자산
        explanation = await generate_explanation(
            snap=snap,
            analysis=result,
            analysis_run_id=run_id,
            shipment_code=event.shipment_code,
            now=finished_at,
        )
        if not isinstance(explanation, ExplanationPassed):
            raise DemoSeedError(
                f"{event.shipment_code}: 설명 검증 실패 {explanation.failure_message}"
            )

    history_run = {
        "id": run_id,
        "label": event.shipment_code,
        "company_id": str(company_id),
        "event_id": str(event.delay_event_id),
        "version": run.version_no,
        "parent": None,
        "rule_id": str(run.operating_rule_set_id),
        "input_seq": run.input_seq,
        "trigger_type": run.trigger_type,
        "trigger_reason": run.trigger_reason,
        "trigger_snapshot": None,
        "status": status,
        "completed_status": status,
        "is_latest": True,
        "scope": result["scope"],
        "signature": result["signature"],
        "sha": result["sha"],
        "confirm_t": confirm_t,
        "result": result,
        "snap": snap,
    }
    rows = engine.storage.rows_for(
        {"events": [], "runs": [history_run], "drafts": [], "finals": []}
    )

    await lock_analysis_run(db, run.analysis_run_id)
    await db.refresh(run)
    if run.status not in IN_PROGRESS:
        raise DemoSeedError(
            f"{event.shipment_code}: 실행이 진행 중이 아닙니다({run.status})"
        )
    tables = Base.metadata.tables
    for (
        _,
        table_name,
        row,
    ) in rows:  # 위험 주문 → 대응안 → 주문 결과 순서(rows_for 순서)
        if table_name in RESULT_TABLES:
            table = tables[table_name]
            await db.execute(insert(table).values(**_typed(table, row)))
    run.status = status
    run.completed_status = status
    run.result_class = result["result_class"]
    run.status_reason = (
        NO_RISK_REASON.get(result["result_class"]) if status == "NO_RISK" else None
    )
    run.dependency_scope = result["scope"]
    run.decision_signature = result["signature"]
    run.decision_signature_sha256 = result["sha"]
    if explanation is not None:
        run.model_name = explanation.model_name
        run.prompt_version = explanation.prompt_version
        run.final_explanation_json = explanation.final_explanation_json
        run.explanation_validation_status = "PASSED"
        run.explanation_generated_at = explanation.generated_at
    run.finished_at = finished_at
    # 데이터 버전은 결과를 저장할 때 기록한다(서버 outcome_save 와 같은 규칙, 보류는 위에서 멈춤)
    await record_input_data_versions(db, run)
    await db.flush()
    return SeededRun(
        shipment_code=event.shipment_code,
        stage=event.stage,
        run_code=run.run_code,
        status=status,
        result_class=result["result_class"],
        options=tuple(
            (o["option_type"], o["status"], o["reason_code"], o["recommended"])
            for o in result["options"]
        ),
    )


# --- 전체 ----------------------------------------------------------------------------------------


async def seed_demo(
    db: AsyncSession,
    open_session: SessionFactory,
    *,
    company_code: str,
    scenario: VirtualTmsScenario,
    virtual_start: datetime,
    engine: Engine,
    supplement: dict | None = None,
) -> list[SeededRun]:
    """데모 시드 한 번. 커밋은 부르는 쪽(수동 수집은 수집 서비스가 직접 커밋)."""
    company_id = await db.scalar(
        text("SELECT company_id FROM companies WHERE company_code = :c"),
        {"c": company_code},
    )
    if company_id is None:
        raise DemoSeedError(f"고객사 {company_code} 가 없습니다(확정 DB 적재 확인)")
    if supplement is not None:
        if supplement["company_code"] != company_code:
            raise DemoSeedError(
                f"보완 파일 고객사 {supplement['company_code']} ≠ {company_code}"
            )
        await load_supplement(db, company_id, supplement)
    await sync_scenario(
        db, open_session, company_id, company_code, scenario, virtual_start
    )
    latest = await db.scalar(_LATEST_INVENTORY_DATE, {"c": company_id})
    if (
        latest is None
    ):  # 재고가 없으면 기준일을 정할 수 없음 (PR #80 리뷰: None + timedelta 오류 대신 알림)
        raise DemoSeedError(
            f"고객사 {company_code} 의 재고 행이 없습니다(확정 DB 적재 확인)"
        )
    basis_day = latest + timedelta(days=1)
    seeded = []
    for event in (await db.execute(_OPEN_EVENTS, {"c": company_id})).all():
        run = await ensure_run(db, company_id, event)
        if run is None:
            continue  # 이미 결과가 있음
        seeded.append(await compute_and_save(db, engine, run, event, basis_day))
    return seeded


def _print(seeded: list[SeededRun]) -> None:
    if not seeded:
        print("새로 계산한 실행이 없습니다(이미 시드됨).")
    for s in seeded:
        print(f"{s.shipment_code} {s.stage} {s.run_code}: {s.status} {s.result_class}")
        for option_type, feasibility, reason, recommended in s.options:
            mark = " (추천)" if recommended else ""
            print(f"    {option_type} {feasibility} {reason or ''}{mark}")


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="데모 시드: 열린 지연과 선택 대기 실행"
    )
    parser.add_argument(
        "--company", default=None, help="고객사 코드(기본: 시나리오의 첫 고객사)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="모두 되돌린다(DB 그대로)"
    )
    parser.add_argument(
        "--supplement",
        default=None,
        help="보완 하루 파일(census extend_day.py 결과 JSON). 주면 데모 수집 전에 관측일 다섯 원천 수집을 넣는다",
    )
    args = parser.parse_args()
    supplement = (
        json.loads(Path(args.supplement).read_text(encoding="utf-8"))
        if args.supplement
        else None
    )

    settings = get_settings()
    if settings.virtual_tms_scenario_path is None:
        print("VIRTUAL_TMS_SCENARIO_PATH 가 없습니다.", file=sys.stderr)
        return 2
    if settings.virtual_clock_start is None:
        print(
            "VIRTUAL_CLOCK_START 가 비어 있습니다(데모는 가상 시각).", file=sys.stderr
        )
        return 2
    scenario = VirtualTmsScenario.from_file(settings.virtual_tms_scenario_path)
    company_code = args.company or scenario.responses[0].company_code
    engine = Engine.load()

    db_engine = create_async_engine(settings.database_url)
    try:
        async with db_engine.connect() as conn:
            outer = await conn.begin()
            # 수집 서비스가 직접 커밋하므로 SAVEPOINT 모드: 끝에서 바깥 트랜잭션을 커밋하거나(실제) 되돌린다(--dry-run)
            db = AsyncSession(bind=conn, join_transaction_mode="create_savepoint")

            @asynccontextmanager
            async def same_session():
                yield db

            try:
                seeded = await seed_demo(
                    db,
                    same_session,
                    company_code=company_code,
                    scenario=scenario,
                    virtual_start=settings.virtual_clock_start,
                    engine=engine,
                    supplement=supplement,
                )
                await db.flush()
            except DemoSeedError as error:
                await outer.rollback()
                print(f"중단(되돌림): {error}", file=sys.stderr)
                return 1
            _print(seeded)
            if args.dry_run:
                await outer.rollback()
                print("--dry-run: 모두 되돌렸습니다.")
            else:
                await outer.commit()
                print(f"{len(seeded)}건 저장했습니다.")
            await db.close()
    finally:
        await db_engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
