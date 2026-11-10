import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.locks import lock_company
from app.core.rls_context import set_rls_context
from app.modules.data_sync.application.exception.data_sync_exceptions import (
    SyncInProgressException,
)
from app.modules.data_sync.application.ingest.change_detection import (
    ChangeRecordDraft,
    diff_inventory,
    diff_order,
    diff_shipment,
    diff_sku,
    diff_warehouse,
)
from app.modules.data_sync.application.ingest.code_resolver import (
    Rejection,
    ShipmentPlan,
    inventory_issue_keys,
    inventory_record_key,
    resolve_inventory,
    resolve_order,
    resolve_shipment,
)
from app.modules.data_sync.application.ingest.record_validation import (
    validate_inventory,
    validate_order,
    validate_shipment,
    validate_sku,
    validate_warehouse,
)
from app.modules.data_sync.application.ingest.run_counts import RunCounts
from app.modules.data_sync.application.ingest.source_payload import (
    SourceInventory,
    SourceOrder,
    SourceRecord,
    SourceShipment,
    SourceSku,
    SourceWarehouse,
)
from app.modules.data_sync.infrastructure.data_sync_enums import (
    ConnectionMethod,
    DataSyncRunStatus,
    SourceDomain,
)
from app.modules.data_sync.infrastructure.repository.ingest_repository import (
    IngestRepository,
)
from app.modules.delay_analysis.application.detection.episode_service import (
    EpisodeDetectionService,
    EpisodeOutcome,
)
from app.modules.delay_analysis.application.run_creation.auto_run_service import (
    AutoRunService,
    SkippedAutoRun,
)
from app.modules.delay_analysis.application.stale.stale_service import (
    StaleJudgmentService,
)
from app.modules.delay_analysis.infrastructure.repository.delay_event_write_repository import (
    DelayEventWriteRepository,
)

"""
    수집 적재 서비스 (GLOW-13, serviceflow 5.2 "수집 적재")
      1. L1 안에서 같은 고객사·원천에 RUNNING 수집이 있는지 보고, 있으면 SYNC_IN_PROGRESS 로 거부합니다.
         없으면 수집 실행을 RUNNING 으로 남기고 커밋합니다 (진행 중 상태가 보이도록).
         같은 원천의 수집은 한 번에 하나만 돌아, 늦게 끝난 수집이 먼저 끝난 수집의 값을 덮어쓰지 않습니다.
         오래 남은 RUNNING(작업이 죽은 경우)도 시간과 관계없이 막습니다. 정리(FAILED 전환·fencing)는 후속 운영 작업입니다.
      2. 한 트랜잭션에서: 고객사 잠금(L1) → 같은 수집 안 중복 거부 → 값 검증(DB 제약 위반 거부, record_validation)
         → 외부 코드 변환(못 바꾸면 거부) → 현재 행 읽기 → 바뀐 행만 적용 → 거부 사유를 검증 이슈로 기록
         → 변경 기록 → 적용 순번 → 수집 완료 기록 → (SHIPMENT 만) 지연 에피소드 판정·시작 저장(detect_after_sync)
         → AUTO 분석 실행 생성·이전 실행 STALE(AutoRunService.create_after_sync, GLOW-18)
         → 에피소드 종료(close_episodes)
         → STALE 판정(judge_after_sync) → 커밋. 지연 감지가 STALE 판정보다 먼저다 (docs/analysis/delay-detection.md 3절).
         1 은 start, 2·3 은 apply 입니다. ingest 는 둘을 이어 부릅니다. 수동 수집(API 25)은 start 를 요청 안에서,
         원천 응답 받기와 apply 를 백그라운드에서 합니다 (그 사이 실패는 fail 로 FAILED 를 남김).
      3. 처리 중 예외가 나면 그 트랜잭션을 되돌리고 수집을 FAILED 로 남긴 뒤 예외를 다시 올립니다.
         FAILED 기록이 또 실패해도 원래 예외를 올리고, 기록 실패는 그 예외의 note 로 붙입니다.
      - 같은 값을 다시 수집하면 업무 행·변경 기록은 그대로이고, 적용 순번만 하나 늘어난 SUCCEEDED 수집이 남습니다.
      - 선적의 시장 리드타임 기준·계획 3PL 입고일은 새 선적일 때만 정하고, 이미 있는 선적은 저장된 값을 씁니다
        (serviceflow v6.2 1절 "계획 입고일"·7절 결정표 "선적 계획 기준").
      - 거부 사유는 결과에 담아 돌려주고, 같은 트랜잭션에서 검증 이슈(data_validation_issues) OPEN 으로 남깁니다 (GLOW-15).
        이번 수집에 정상 적재된 레코드와 (엔터티 유형, 키)가 같은 이전 수집의 OPEN 이슈는 RESOLVED 로 바꿉니다 (해소).

    실행 전제 (호출 측 계약)
      - DB 역할: 워커 역할(glowship_v4_worker) 연결에서 실행합니다. 이 서비스는 RLS 컨텍스트에 고객사만 넣고
        (user_id=None), 워커 역할의 app_context_allowed 는 고객사가 ACTIVE 인지만 봅니다. 앱 역할(glowship_v4_app)은
        ACTIVE 사용자(그 고객사 CLIENT 또는 배정된 ADMIN)도 요구하므로, 앱 역할로 돌리면 행이 모두 숨겨져 조회가 비고
        INSERT 가 고유 제약 위반으로 실패합니다 (마이그레이션 0001 app_context_allowed).
        워커 역할 권한(0001 GRANT)은 이 서비스가 쓰는 표를 포함하고, 지연 에피소드 종료(delay_events 의
        closed_at·close_reason UPDATE)는 마이그레이션 0007(GLOW-117)이 두 열만 줍니다 (ERD 설명 4절).
      - 세션: 이 서비스 전용 세션을 넘깁니다. 서비스가 직접 commit·rollback 하므로(RUNNING 기록, 적용, FAILED 기록)
        요청 세션(get_db)을 공유하면 안 됩니다.
      - 워커 역할 연결은 app/core/database.open_worker_session 입니다(2026-11-05 결정). 설정 WORKER_POSTGRES_USER·PASSWORD 의
        워커 계정으로 붙고, 비어 있으면 앱 연결(postgres_user)과 같습니다. 로컬 테스트는 superuser 연결이라 RLS 를 거치지 않습니다.
      - 파서가 레코드로 바꾸지 못해 거부한 원천 레코드(MISSING_REQUIRED·INVALID_DATE, source_payload 의 파서 계약)는
        parse_rejections 로 받습니다. 받은 건수·거부 건수에 넣고 결과의 거부 사유에도 앞에 담습니다.

    수집 시각 (이슈 #39, 엔진 규칙)
      - 초 단위: now 가 준 시각에서 1초 미만을 버립니다. 엔진 store.iso_ts 가 1초 미만 시각을 거부합니다.
      - 완료 시각 > 시작 시각: finish_after 가 있으면(가상 시각, 30초) 시작 + finish_after, 없으면 max(지금, 시작 + 1초).
        FAILED 완료 시각도 같습니다. 검증 이슈 감지·해소 시각과 STALE 감지 시각은 완료 시각입니다.
      - 겹칠 때: 시작 시각에 그 고객사의 수집이 이미 있으면(원천·상태 무관) 1초씩 밉니다(start, L1 안).
        엔진 store.run_at 이 (company_id, started_at) 키입니다. FAILED 뒤 같은 날 재시도는 00:03:01 이 됩니다.
      - 시작 순서 = 적용 순번: start 가 RUNNING 에 쓴 시작 시각은 임시입니다. apply 가 L1 을 잡은 뒤
        max(임시 시각, 고객사 마지막 적재 수집 시작 + 1초)로 확정해 다시 씁니다(겹치면 1초씩 밂). 일정 스냅샷 관측 시각과
        완료 시각도 확정한 값을 기준으로 합니다. 그래서 수집 도중 GET /data-sync-runs/latest 의 startedAt 은 끝난 뒤와 다를 수 있고,
        FAILED 수집은 임시 시각 그대로 남습니다(적용 트랜잭션을 되돌리므로).
"""

# 확정 기준선(data_validation_issues.csv)과 같은 이름
DUPLICATE_SOURCE_RECORD = "DUPLICATE_SOURCE_RECORD"


def whole_second(value: datetime) -> datetime:
    """1초 미만을 버린 시각. 엔진은 1초 미만 시각을 거부한다(store.iso_ts, 이슈 #39 규칙 1)."""
    return value.replace(microsecond=0)


@dataclass(frozen=True)
class IngestResult:
    data_sync_run_id: uuid.UUID
    status: DataSyncRunStatus
    counts: RunCounts
    applied_seq: int
    change_record_count: int
    rejections: tuple[Rejection, ...]
    staled_run_ids: tuple[uuid.UUID, ...]
    # SHIPMENT 수집의 지연 에피소드 판정 (시작·후속·종료). 다른 원천은 ()
    episode_outcomes: tuple[EpisodeOutcome, ...] = ()
    # 이 수집이 만든 AUTO 분석 실행 (DETECTED, 분석 시작은 GLOW-128). 다른 원천은 ()
    created_run_ids: tuple[uuid.UUID, ...] = ()
    # 실행 생성 오류(운영 기준 없음 등)로 건너뛴 판정. 수집은 그대로 적재된다(#63 리뷰 [권장])
    skipped_auto_runs: tuple[SkippedAutoRun, ...] = ()


@dataclass(frozen=True)
class StartedRun:
    """RUNNING 으로 기록·커밋한 수집 (start 의 결과, apply 에 넘김)"""

    company_id: uuid.UUID
    data_sync_run_id: uuid.UUID
    source_domain: SourceDomain
    # 수집 시작 시각(임시). apply 가 L1 안에서 확정해 다시 쓰고, SHIPMENT 의 일정 스냅샷 관측 시각(observed_at)은 확정한 값이다.
    started_at: datetime


@dataclass(frozen=True)
class _Applied:
    records: list[ChangeRecordDraft]
    rejections: list[Rejection]
    # 이번 수집에서 거부되지 않고 적재된 레코드의 키 (거부 키와 같은 형식). 이전 검증 이슈 해소에 씁니다.
    loaded_keys: list[str]


class DataIngestService:
    def __init__(
        self,
        db: AsyncSession,
        repo: IngestRepository,
        stale_service: StaleJudgmentService,
        episode_service: EpisodeDetectionService | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        finish_after: timedelta | None = None,
        auto_runs: AutoRunService | None = None,
    ):
        self.db = db
        self.repo = repo
        self.stale_service = stale_service
        # 같은 세션·트랜잭션에서 돌아야 하므로 기본값은 이 세션으로 만든다 (테스트는 대역을 넘길 수 있음)
        self.episode_service = episode_service or EpisodeDetectionService(
            DelayEventWriteRepository(db)
        )
        # AUTO 실행 생성(GLOW-18). 이전 실행 STALE 은 이 세션의 실제 STALE 서비스로 한다(stale_service 대역과 무관)
        self.auto_runs = auto_runs or AutoRunService(db)
        # 수집이 쓰는 시각(시작·완료, 일정 스냅샷 관측, 검증 이슈 감지·해소, STALE 판정)은 모두 이 값이라 여기서 초 단위로 맞춘다 (이슈 #39 규칙 1)
        self.now = lambda: whole_second(now())
        # 가상 시각(수동 수집)은 완료 = 시작 + 30초. None 이면 지금 시각 (규칙 3)
        self.finish_after = finish_after

    def _finished_at(self, started_at: datetime) -> datetime:
        """완료 시각 > 시작 시각 (이슈 #39 규칙 3). 엔진 stale.judge 는 완료 시각으로 수집을 찾는다."""
        if self.finish_after is not None:
            return started_at + self.finish_after
        return max(self.now(), started_at + timedelta(seconds=1))

    async def ingest(
        self,
        company_id: uuid.UUID,
        source_domain: SourceDomain,
        connection_method: ConnectionMethod,
        records: Sequence[SourceRecord],
        parse_rejections: Sequence[Rejection] = (),
    ) -> IngestResult:
        """시작(start)과 적용(apply)을 이어서 한 번에 실행합니다 (원천 응답을 이미 받은 호출 측)."""
        started = await self.start(company_id, source_domain, connection_method)
        return await self.apply(started, records, parse_rejections)

    async def start(
        self,
        company_id: uuid.UUID,
        source_domain: SourceDomain,
        connection_method: ConnectionMethod,
    ) -> StartedRun:
        """
        1단계: L1 안에서 같은 고객사·원천에 RUNNING 수집이 있으면 SYNC_IN_PROGRESS 로 거부하고,
        없으면 수집을 RUNNING 으로 남기고 커밋합니다. 수동 수집(API 25)은 이 단계를 요청 안에서 해
        실행 ID 와 409 를 바로 응답하고, 원천 응답을 받아 apply 하는 일은 백그라운드에서 합니다.
        apply 를 부르지 못하고 끝나면(원천 호출 실패 등) fail 로 FAILED 를 남겨야 다음 수집이 막히지 않습니다.
        """
        await set_rls_context(self.db, user_id=None, company_id=company_id)
        await lock_company(self.db, company_id)
        if await self.repo.has_running_run(company_id, source_domain):
            await self.db.rollback()
            raise SyncInProgressException()
        # 그 고객사의 다른 수집과 시작 초가 겹치면 1초씩 민다 (규칙 4, L1 안)
        started_at = await self.repo.free_started_at(company_id, self.now())
        run_id = await self.repo.start_run(
            company_id, source_domain, connection_method, started_at
        )
        await self.db.commit()
        return StartedRun(
            company_id=company_id,
            data_sync_run_id=run_id,
            source_domain=source_domain,
            started_at=started_at,
        )

    async def apply(
        self,
        started: StartedRun,
        records: Sequence[SourceRecord],
        parse_rejections: Sequence[Rejection] = (),
    ) -> IngestResult:
        """2단계: 한 트랜잭션에서 적용하고 완료를 기록합니다. 실패하면 되돌리고 FAILED 를 남긴 뒤 예외를 다시 올립니다.
        start 와 다른 세션(같은 DB)에서 불러도 됩니다."""
        company_id = started.company_id
        run_id = started.data_sync_run_id
        source_domain = started.source_domain
        try:
            await set_rls_context(self.db, user_id=None, company_id=company_id)
            await lock_company(self.db, company_id)
            started_at = await self._confirm_started_at(started)
            applied = await self._apply(
                company_id, run_id, source_domain, records, started_at
            )
            rejections = (*parse_rejections, *applied.rejections)
            # 검증 이슈의 감지·해소 시각과 수집 완료 시각은 같은 값입니다 (확정 기준선 이슈 9행 모두 detected_at = 수집 finished_at)
            finished_at = self._finished_at(started_at)
            await self.repo.resolve_validation_issues(
                company_id,
                run_id,
                source_domain,
                applied.loaded_keys,
                rejections,
                finished_at,
            )
            await self.repo.insert_validation_issues(
                company_id, run_id, source_domain, rejections, finished_at
            )
            counts = RunCounts(
                received=len(records) + len(parse_rejections),
                loaded=len(records) - len(applied.rejections),
                rejected=len(rejections),
            )
            applied_seq = await self.repo.next_applied_seq(company_id)
            await self.repo.complete_run(
                company_id, run_id, counts, applied_seq, finished_at
            )
            episodes = (
                await self.episode_service.detect_after_sync(company_id, run_id)
                if source_domain == SourceDomain.SHIPMENT
                else []
            )
            # AUTO 실행 생성·이전 실행 STALE(GLOW-18, 이슈 #34): 에피소드를 닫기 전에 한다.
            # 닫힌 에피소드의 실행은 동결이라 mark_runs_stale 이 건너뛰기 때문이다 (episode_service 설명).
            # 실행 시각 = 이 수집의 확정 시작 시각(= 고객사 마지막 적재 수집 시작, 이슈 #50)
            auto_runs = await self.auto_runs.create_after_sync(
                company_id,
                episodes,
                applied_seq=applied_seq,
                started_at=started_at,
                finished_at=finished_at,
            )
            await self.episode_service.close_episodes(company_id, episodes)
            # STALE 감지 시각도 수집 완료 시각 (검증 이슈 감지 시각과 같은 규칙)
            staled = await self.stale_service.judge_after_sync(
                company_id, applied_seq, finished_at
            )
            await self.db.commit()
        except Exception as error:
            await self.fail(started, error)
            raise

        return IngestResult(
            data_sync_run_id=run_id,
            status=counts.status,
            counts=counts,
            applied_seq=applied_seq,
            change_record_count=len(applied.records),
            rejections=rejections,
            staled_run_ids=tuple(run.analysis_run_id for run in staled),
            episode_outcomes=tuple(episodes),
            created_run_ids=auto_runs.created,
            skipped_auto_runs=auto_runs.skipped,
        )

    async def _confirm_started_at(self, started: StartedRun) -> datetime:
        """
        시작 시각 확정 (이슈 #39 규칙 5, L1 안). start 가 RUNNING 에 쓴 시각은 임시이고, 적용 순번을 받기 직전인 지금
        "그 고객사의 마지막 적재 수집 시작 + 1초" 보다 늦게 다시 정합니다(겹치면 1초씩 밂). 그래서 시작 시각 순서가
        적용 순번(applied_seq) 순서와 같아집니다. 엔진 input_seq 는 "시작 시각 ≤ t 인 적재 수집의 최대 순번"이라
        먼저 시작해 L1 을 기다린 수집이 늦게 적용되면 PR #36 check_input_seq 가 멈춥니다.
        """
        candidate = started.started_at
        last = await self.repo.last_applied_started_at(started.company_id)
        if last is not None and candidate <= last:
            candidate = last + timedelta(seconds=1)
        confirmed = await self.repo.free_started_at(
            started.company_id, candidate, exclude_run_id=started.data_sync_run_id
        )
        if confirmed != started.started_at:
            await self.repo.move_started_at(
                started.company_id, started.data_sync_run_id, confirmed
            )
        return confirmed

    async def fail(self, started: StartedRun, error: BaseException) -> None:
        """진행 중 트랜잭션을 되돌리고 수집을 FAILED 로 남깁니다. 기록이 또 실패해도 예외를 올리지 않고
        원인 예외(error)에 note 로 붙입니다(호출 측이 원인 예외를 올림)."""
        try:
            await self.db.rollback()
            await set_rls_context(self.db, user_id=None, company_id=started.company_id)
            await self.repo.fail_run(
                started.company_id,
                started.data_sync_run_id,
                self._finished_at(started.started_at),
            )
            await self.db.commit()
        except Exception as record_error:  # noqa: BLE001 어떤 기록 실패든 원래 예외를 지킴
            # 실패 기록이 또 실패해도(연결 끊김, 이미 RUNNING 아님 등) 원인인 원래 예외를 올립니다
            error.add_note(
                f"수집 {started.data_sync_run_id} 의 FAILED 기록도 실패했습니다: {record_error!r}"
            )

    async def _apply(
        self,
        company_id: uuid.UUID,
        run_id: uuid.UUID,
        source_domain: SourceDomain,
        records: Sequence[SourceRecord],
        observed_at: datetime,
    ) -> _Applied:
        match source_domain:
            case SourceDomain.PRODUCT:
                applied = await self._apply_skus(company_id, records)
            case SourceDomain.WAREHOUSE:
                applied = await self._apply_warehouses(company_id, records)
            case SourceDomain.ORDER:
                applied = await self._apply_orders(company_id, run_id, records)
            case SourceDomain.SHIPMENT:
                applied = await self._apply_shipments(
                    company_id, run_id, records, observed_at
                )
            case SourceDomain.INVENTORY:
                applied = await self._apply_inventory(company_id, run_id, records)
        await self.repo.insert_change_records(company_id, run_id, applied.records)
        return applied

    async def _apply_skus(
        self, company_id: uuid.UUID, records: Sequence[SourceSku]
    ) -> _Applied:
        unique, rejections = _unique(records, lambda r: r.sku_code)
        unique = _validated(unique, validate_sku, rejections)
        existing = await self.repo.load_skus(company_id, list(unique))
        diffs = [
            d for code, r in unique.items() if (d := diff_sku(existing.get(code), r))
        ]
        await self.repo.apply_skus(company_id, [d.row for d in diffs])
        return _Applied([x for d in diffs for x in d.records], rejections, list(unique))

    async def _apply_warehouses(
        self, company_id: uuid.UUID, records: Sequence[SourceWarehouse]
    ) -> _Applied:
        unique, rejections = _unique(records, lambda r: r.warehouse_code)
        unique = _validated(unique, validate_warehouse, rejections)
        existing = await self.repo.load_warehouses(company_id, list(unique))
        diffs = [
            d
            for code, r in unique.items()
            if (d := diff_warehouse(existing.get(code), r))
        ]
        await self.repo.apply_warehouses(company_id, [d.row for d in diffs])
        return _Applied([x for d in diffs for x in d.records], rejections, list(unique))

    async def _apply_orders(
        self, company_id: uuid.UUID, run_id: uuid.UUID, records: Sequence[SourceOrder]
    ) -> _Applied:
        unique, rejections = _unique(records, lambda r: r.order_code)
        unique = _validated(unique, validate_order, rejections)
        book = await self.repo.load_code_book(company_id, SourceDomain.ORDER)
        resolved = []
        for source in unique.values():
            result = resolve_order(source, book)
            if isinstance(result, Rejection):
                rejections.append(result)
            else:
                resolved.append(result)
        existing = await self.repo.load_orders(
            company_id, [r.order_code for r in resolved]
        )
        diffs = [
            d for r in resolved if (d := diff_order(existing.get(r.order_code), r))
        ]
        await self.repo.apply_orders(company_id, run_id, diffs)
        return _Applied(
            [x for d in diffs for x in d.records],
            rejections,
            [r.order_code for r in resolved],
        )

    async def _apply_shipments(
        self,
        company_id: uuid.UUID,
        run_id: uuid.UUID,
        records: Sequence[SourceShipment],
        observed_at: datetime,
    ) -> _Applied:
        unique, rejections = _unique(records, lambda r: r.shipment_code)
        unique = _validated(unique, validate_shipment, rejections)
        book = await self.repo.load_code_book(company_id, SourceDomain.SHIPMENT)
        existing = await self.repo.load_shipments(company_id, list(unique))
        resolved = []
        for code, source in unique.items():
            current = existing.get(code)
            plan = (
                ShipmentPlan(
                    current.record.market_leadtime_reference_id,
                    current.record.planned_3pl_inbound_date,
                )
                if current
                else None
            )
            result = resolve_shipment(source, book, plan)
            if isinstance(result, Rejection):
                rejections.append(result)
            else:
                resolved.append(result)
        diffs = [
            d
            for r in resolved
            if (d := diff_shipment(existing.get(r.shipment_code), r))
        ]
        await self.repo.apply_shipments(company_id, run_id, observed_at, diffs)
        return _Applied(
            [x for d in diffs for x in d.records],
            rejections,
            [r.shipment_code for r in resolved],
        )

    async def _apply_inventory(
        self,
        company_id: uuid.UUID,
        run_id: uuid.UUID,
        records: Sequence[SourceInventory],
    ) -> _Applied:
        book = await self.repo.load_code_book(company_id, SourceDomain.INVENTORY)
        unique, rejections = _unique(
            records,
            lambda r: (r.inventory_date, r.warehouse_code, r.sku_code),
            lambda r: inventory_record_key(r, book),
        )
        unique = _validated(
            unique,
            lambda r: validate_inventory(r, inventory_record_key(r, book)),
            rejections,
        )
        # 외부 코드 매핑은 N → 1 을 막지 않습니다. 서로 다른 외부 코드가 같은 내부 (날짜, 창고, SKU)로 바뀌면
        # 첫 레코드만 적재하고 나머지는 거부합니다 (그대로 두면 inventory_daily_uk2 위반으로 수집 전체가 실패).
        resolved, keys, seen, loaded_keys = [], [], set(), []
        for source in unique.values():
            result = resolve_inventory(source, book)
            if isinstance(result, Rejection):
                rejections.append(result)
                continue
            key = (result.inventory_date, result.warehouse_id, result.sku_id)
            if key in seen:
                rejections.append(
                    Rejection(
                        source_record_key=inventory_record_key(source, book),
                        field_name="source_record_key",
                        error_code=DUPLICATE_SOURCE_RECORD,
                        error_message="다른 외부 코드가 같은 내부 재고 행(날짜·창고·SKU)으로 바뀜"
                        " (첫 레코드만 적재)",
                    )
                )
                continue
            resolved.append(result)
            keys.append(key)
            seen.add(key)
            loaded_keys.extend(inventory_issue_keys(source, book))
        existing = await self.repo.load_inventory(company_id, keys)
        diffs = [
            d
            for key, r in zip(keys, resolved, strict=True)
            if (d := diff_inventory(existing.get(key), r))
        ]
        await self.repo.apply_inventory(company_id, run_id, [d.row for d in diffs])
        return _Applied([x for d in diffs for x in d.records], rejections, loaded_keys)


def _validated[R](
    unique: dict[object, R],
    validate: Callable[[R], Rejection | None],
    rejections: list[Rejection],
) -> dict[object, R]:
    """값 검증을 통과한 레코드만 남기고, 어긴 레코드의 거부 사유는 rejections 에 더합니다."""
    valid = {}
    for k, record in unique.items():
        if (rejection := validate(record)) is None:
            valid[k] = record
        else:
            rejections.append(rejection)
    return valid


def _unique[R](
    records: Sequence[R],
    key: Callable[[R], object],
    record_key: Callable[[R], str] | None = None,
) -> tuple[dict, list[Rejection]]:
    """
    같은 수집 안에서 업무 키가 겹치면 첫 레코드만 적재하고, 두 번째부터는 DUPLICATE_SOURCE_RECORD 로 거부합니다
    (확정 기준선 data_validation_issues.csv 의 "첫 레코드만 적재"와 같음).
    거부 키는 record_key 가 있으면 그 형식(재고: code_resolver.inventory_record_key), 없으면 업무 키 문자열입니다.
    """
    unique, rejections = {}, []
    for record in records:
        k = key(record)
        if k not in unique:
            unique[k] = record
            continue
        rejections.append(
            Rejection(
                source_record_key=record_key(record) if record_key else str(k),
                field_name="source_record_key",
                error_code=DUPLICATE_SOURCE_RECORD,
                error_message="같은 수집에 같은 업무 키가 이미 있음 (첫 레코드만 적재)",
            )
        )
    return unique, rejections
