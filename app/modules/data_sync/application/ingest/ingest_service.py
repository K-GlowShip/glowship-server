import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

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
from app.modules.delay_analysis.application.stale.stale_service import (
    StaleJudgmentService,
)

"""
    수집 적재 서비스 (GLOW-13, serviceflow 5.2 "수집 적재")
      1. L1 안에서 같은 고객사·원천에 RUNNING 수집이 있는지 보고, 있으면 SYNC_IN_PROGRESS 로 거부합니다.
         없으면 수집 실행을 RUNNING 으로 남기고 커밋합니다 (진행 중 상태가 보이도록).
         같은 원천의 수집은 한 번에 하나만 돌아, 늦게 끝난 수집이 먼저 끝난 수집의 값을 덮어쓰지 않습니다.
         오래 남은 RUNNING(작업이 죽은 경우)도 시간과 관계없이 막습니다. 정리(FAILED 전환·fencing)는 후속 운영 작업입니다.
      2. 한 트랜잭션에서: 고객사 잠금(L1) → 같은 수집 안 중복 거부 → 값 검증(DB 제약 위반 거부, record_validation)
         → 외부 코드 변환(못 바꾸면 거부) → 현재 행 읽기 → 바뀐 행만 적용
         → 변경 기록 → 적용 순번 → 수집 완료 기록 → STALE 판정(judge_after_sync) → 커밋.
      3. 처리 중 예외가 나면 그 트랜잭션을 되돌리고 수집을 FAILED 로 남긴 뒤 예외를 다시 올립니다.
         FAILED 기록이 또 실패해도 원래 예외를 올리고, 기록 실패는 그 예외의 note 로 붙입니다.
      - 같은 값을 다시 수집하면 업무 행·변경 기록은 그대로이고, 적용 순번만 하나 늘어난 SUCCEEDED 수집이 남습니다.
      - 선적의 시장 리드타임 기준·계획 3PL 입고일은 새 선적일 때만 정하고, 이미 있는 선적은 저장된 값을 씁니다
        (serviceflow v6.2 1절 "계획 입고일"·7절 결정표 "선적 계획 기준").
      - 거부 사유는 결과에 담아 돌려줍니다. 검증 오류 기록(data_validation_issues)은 GLOW-15 가 붙입니다.

    실행 전제 (호출 측 계약)
      - DB 역할: 워커 역할(glowship_v4_worker) 연결에서 실행합니다. 이 서비스는 RLS 컨텍스트에 고객사만 넣고
        (user_id=None), 워커 역할의 app_context_allowed 는 고객사가 ACTIVE 인지만 봅니다. 앱 역할(glowship_v4_app)은
        ACTIVE 사용자(그 고객사 CLIENT 또는 배정된 ADMIN)도 요구하므로, 앱 역할로 돌리면 행이 모두 숨겨져 조회가 비고
        INSERT 가 고유 제약 위반으로 실패합니다 (마이그레이션 0001 app_context_allowed).
        워커 역할 권한(0001 GRANT)은 이 서비스가 쓰는 표를 모두 포함합니다.
      - 세션: 이 서비스 전용 세션을 넘깁니다. 서비스가 직접 commit·rollback 하므로(RUNNING 기록, 적용, FAILED 기록)
        요청 세션(get_db)을 공유하면 안 됩니다.
      - 지금 앱 연결은 설정의 postgres_user 하나뿐이고 워커 역할 연결은 아직 없습니다. 수동 수집 API 25
        (POST /data-sync-runs, 202 뒤 백그라운드 실행)를 붙일 때 워커 역할 전용 세션을 새로 여는 방식으로 연결을 정합니다
        (아직 확정 아님). 로컬 테스트는 superuser 연결이라 RLS 를 거치지 않습니다.
      - 파서가 레코드로 바꾸지 못해 거부한 원천 레코드(MISSING_REQUIRED·INVALID_DATE, source_payload 의 파서 계약)는
        parse_rejections 로 받습니다. 받은 건수·거부 건수에 넣고 결과의 거부 사유에도 앞에 담습니다.
"""

# 확정 기준선(data_validation_issues.csv)과 같은 이름
DUPLICATE_SOURCE_RECORD = "DUPLICATE_SOURCE_RECORD"


@dataclass(frozen=True)
class IngestResult:
    data_sync_run_id: uuid.UUID
    status: DataSyncRunStatus
    counts: RunCounts
    applied_seq: int
    change_record_count: int
    rejections: tuple[Rejection, ...]
    staled_run_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True)
class _Applied:
    records: list[ChangeRecordDraft]
    rejections: list[Rejection]


class DataIngestService:
    def __init__(
        self,
        db: AsyncSession,
        repo: IngestRepository,
        stale_service: StaleJudgmentService,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.db = db
        self.repo = repo
        self.stale_service = stale_service
        self.now = now

    async def ingest(
        self,
        company_id: uuid.UUID,
        source_domain: SourceDomain,
        connection_method: ConnectionMethod,
        records: Sequence[SourceRecord],
        parse_rejections: Sequence[Rejection] = (),
    ) -> IngestResult:
        started_at = self.now()
        await set_rls_context(self.db, user_id=None, company_id=company_id)
        await lock_company(self.db, company_id)
        if await self.repo.has_running_run(company_id, source_domain):
            await self.db.rollback()
            raise SyncInProgressException()
        run_id = await self.repo.start_run(
            company_id, source_domain, connection_method, started_at
        )
        await self.db.commit()

        try:
            await set_rls_context(self.db, user_id=None, company_id=company_id)
            await lock_company(self.db, company_id)
            applied = await self._apply(
                company_id, run_id, source_domain, records, started_at
            )
            rejections = (*parse_rejections, *applied.rejections)
            counts = RunCounts(
                received=len(records) + len(parse_rejections),
                loaded=len(records) - len(applied.rejections),
                rejected=len(rejections),
            )
            applied_seq = await self.repo.next_applied_seq(company_id)
            await self.repo.complete_run(
                company_id, run_id, counts, applied_seq, self.now()
            )
            staled = await self.stale_service.judge_after_sync(
                company_id, applied_seq, self.now()
            )
            await self.db.commit()
        except Exception as error:
            try:
                await self.db.rollback()
                await set_rls_context(self.db, user_id=None, company_id=company_id)
                await self.repo.fail_run(company_id, run_id, self.now())
                await self.db.commit()
            except Exception as record_error:  # noqa: BLE001 어떤 기록 실패든 원래 예외를 지킴
                # 실패 기록이 또 실패해도(연결 끊김, 이미 RUNNING 아님 등) 원인인 원래 예외를 올립니다
                error.add_note(
                    f"수집 {run_id} 의 FAILED 기록도 실패했습니다: {record_error!r}"
                )
            raise

        return IngestResult(
            data_sync_run_id=run_id,
            status=counts.status,
            counts=counts,
            applied_seq=applied_seq,
            change_record_count=len(applied.records),
            rejections=rejections,
            staled_run_ids=tuple(run.analysis_run_id for run in staled),
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
        return _Applied([x for d in diffs for x in d.records], rejections)

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
        return _Applied([x for d in diffs for x in d.records], rejections)

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
        return _Applied([x for d in diffs for x in d.records], rejections)

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
        return _Applied([x for d in diffs for x in d.records], rejections)

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
        resolved, keys, seen = [], [], set()
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
        existing = await self.repo.load_inventory(company_id, keys)
        diffs = [
            d
            for key, r in zip(keys, resolved, strict=True)
            if (d := diff_inventory(existing.get(key), r))
        ]
        await self.repo.apply_inventory(company_id, run_id, [d.row for d in diffs])
        return _Applied([x for d in diffs for x in d.records], rejections)


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
