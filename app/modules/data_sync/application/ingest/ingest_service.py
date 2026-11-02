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
    resolve_inventory,
    resolve_order,
    resolve_shipment,
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
      2. 한 트랜잭션에서: 고객사 잠금(L1) → 외부 코드 변환(못 바꾸면 거부) → 현재 행 읽기 → 바뀐 행만 적용
         → 변경 기록 → 적용 순번 → 수집 완료 기록 → STALE 판정(judge_after_sync) → 커밋.
      3. 처리 중 예외가 나면 그 트랜잭션을 되돌리고 수집을 FAILED 로 남긴 뒤 예외를 다시 올립니다.
      - 같은 값을 다시 수집하면 업무 행·변경 기록은 그대로이고, 적용 순번만 하나 늘어난 SUCCEEDED 수집이 남습니다.
      - 선적의 시장 리드타임 기준·계획 3PL 입고일은 새 선적일 때만 정하고, 이미 있는 선적은 저장된 값을 씁니다 (1′).
      - 거부 사유는 결과에 담아 돌려줍니다. 검증 오류 기록(data_validation_issues)은 GLOW-15 가 붙입니다.
"""

DUPLICATE_IN_BATCH = "DUPLICATE_IN_BATCH"


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
            counts = RunCounts(
                received=len(records),
                loaded=len(records) - len(applied.rejections),
                rejected=len(applied.rejections),
            )
            applied_seq = await self.repo.next_applied_seq(company_id)
            await self.repo.complete_run(
                company_id, run_id, counts, applied_seq, self.now()
            )
            staled = await self.stale_service.judge_after_sync(
                company_id, applied_seq, self.now()
            )
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            await set_rls_context(self.db, user_id=None, company_id=company_id)
            await self.repo.fail_run(company_id, run_id, self.now())
            await self.db.commit()
            raise

        return IngestResult(
            data_sync_run_id=run_id,
            status=counts.status,
            counts=counts,
            applied_seq=applied_seq,
            change_record_count=len(applied.records),
            rejections=tuple(applied.rejections),
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
        unique, rejections = _unique(
            records, lambda r: (r.inventory_date, r.warehouse_code, r.sku_code)
        )
        book = await self.repo.load_code_book(company_id, SourceDomain.INVENTORY)
        resolved = []
        for source in unique.values():
            result = resolve_inventory(source, book)
            if isinstance(result, Rejection):
                rejections.append(result)
            else:
                resolved.append(result)
        keys = [(r.inventory_date, r.warehouse_id, r.sku_id) for r in resolved]
        existing = await self.repo.load_inventory(company_id, keys)
        diffs = [
            d
            for key, r in zip(keys, resolved, strict=True)
            if (d := diff_inventory(existing.get(key), r))
        ]
        await self.repo.apply_inventory(company_id, run_id, [d.row for d in diffs])
        return _Applied([x for d in diffs for x in d.records], rejections)


def _unique[R](
    records: Sequence[R], key: Callable[[R], object]
) -> tuple[dict, list[Rejection]]:
    """같은 수집 안에서 업무 키가 겹치는 레코드는 모두 거부합니다 (어느 값이 맞는지 알 수 없음)."""
    counts: dict[object, int] = {}
    for record in records:
        counts[key(record)] = counts.get(key(record), 0) + 1
    unique, rejections = {}, []
    for record in records:
        k = key(record)
        if counts[k] == 1:
            unique[k] = record
        else:
            rejections.append(
                Rejection(
                    source_record_key=" ".join(map(str, k))
                    if isinstance(k, tuple)
                    else str(k),
                    field_name="source_record_key",
                    error_code=DUPLICATE_IN_BATCH,
                    error_message=f"같은 수집에 같은 업무 키가 {counts[k]}건 있음",
                )
            )
    return unique, rejections
