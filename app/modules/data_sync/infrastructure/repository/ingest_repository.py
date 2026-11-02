import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import date, datetime

from sqlalchemy import bindparam, func, insert, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.data_sync.application.ingest.change_detection import (
    ChangeRecordDraft,
    Existing,
    ExistingOrder,
    ExistingShipment,
    OrderDiff,
    RowChange,
    ShipmentDiff,
)
from app.modules.data_sync.application.ingest.run_counts import RunCounts
from app.modules.data_sync.application.ingest.source_records import (
    InventoryRecord,
    OrderLineRecord,
    OrderRecord,
    ShipmentLineRecord,
    ShipmentRecord,
    SkuRecord,
    WarehouseRecord,
)
from app.modules.data_sync.infrastructure.data_change_record import DataChangeRecord
from app.modules.data_sync.infrastructure.data_sync_enums import (
    ConnectionMethod,
    DataSyncRunStatus,
    SourceDomain,
)
from app.modules.data_sync.infrastructure.data_sync_run import DataSyncRun
from app.modules.inventory.infrastructure.inventory_daily import InventoryDaily
from app.modules.master_data.infrastructure.sku import Sku
from app.modules.master_data.infrastructure.warehouse import Warehouse
from app.modules.order.infrastructure.order import Order
from app.modules.order.infrastructure.order_line import OrderLine
from app.modules.shipment.infrastructure.shipment import Shipment
from app.modules.shipment.infrastructure.shipment_line import ShipmentLine
from app.modules.shipment.infrastructure.shipment_schedule_snapshot import (
    ShipmentScheduleSnapshot,
)

"""
    수집 적재 Repository (GLOW-13, serviceflow 5.2 "수집 적재")
      - 원천 레코드의 업무 키로 현재 행을 읽고(load_*), 변경 판정(change_detection) 결과를 적용합니다(apply_*).
      - 업무 행의 data_sync_run_id 는 그 행을 마지막으로 바꾼 수집입니다. 상세만 바뀌어도 상위 주문·선적의 수집 ID 를 바꿉니다.
      - 적용 순번(applied_seq)은 고객사 잠금(L1) 안에서 최대값 + 1 로 정합니다. 호출 측이 L1 을 잡고 부릅니다.
      - 모든 조회·변경은 고객사(company_id)로 거릅니다. 커밋은 하지 않습니다 (수집 1회 = 한 트랜잭션, 서비스가 커밋).
"""

_SKU_COLUMNS = ("sku_code", *SkuRecord.FIELDS)
_WAREHOUSE_COLUMNS = ("warehouse_code", *WarehouseRecord.FIELDS)
_ORDER_COLUMNS = ("order_code", *OrderRecord.FIELDS)
_SHIPMENT_COLUMNS = ("shipment_code", *ShipmentRecord.FIELDS)


class IngestRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # --- Read: 업무 키로 현재 행 -----------------------------------------------------

    async def load_skus(
        self, company_id: uuid.UUID, sku_codes: Sequence[str]
    ) -> dict[str, Existing[SkuRecord]]:
        rows = await self.db.execute(
            select(Sku.sku_id, *_columns(Sku, _SKU_COLUMNS)).where(
                Sku.company_id == company_id, Sku.sku_code.in_(sku_codes)
            )
        )
        return {
            row.sku_code: Existing(row.sku_id, _record(SkuRecord, row)) for row in rows
        }

    async def load_warehouses(
        self, company_id: uuid.UUID, warehouse_codes: Sequence[str]
    ) -> dict[str, Existing[WarehouseRecord]]:
        rows = await self.db.execute(
            select(
                Warehouse.warehouse_id, *_columns(Warehouse, _WAREHOUSE_COLUMNS)
            ).where(
                Warehouse.company_id == company_id,
                Warehouse.warehouse_code.in_(warehouse_codes),
            )
        )
        return {
            row.warehouse_code: Existing(
                row.warehouse_id, _record(WarehouseRecord, row)
            )
            for row in rows
        }

    async def load_orders(
        self, company_id: uuid.UUID, order_codes: Sequence[str]
    ) -> dict[str, ExistingOrder]:
        headers = (
            await self.db.execute(
                select(Order.order_id, *_columns(Order, _ORDER_COLUMNS)).where(
                    Order.company_id == company_id, Order.order_code.in_(order_codes)
                )
            )
        ).all()
        if not headers:
            return {}
        lines = await self.db.execute(
            select(
                OrderLine.order_id,
                OrderLine.order_line_id,
                OrderLine.line_no,
                *_columns(OrderLine, OrderLineRecord.FIELDS),
            )
            .where(
                OrderLine.company_id == company_id,
                OrderLine.order_id.in_([h.order_id for h in headers]),
            )
            .order_by(OrderLine.order_id, OrderLine.line_no)
        )
        by_order: defaultdict[uuid.UUID, list] = defaultdict(list)
        for line in lines:
            by_order[line.order_id].append(line)
        return {
            h.order_code: ExistingOrder(
                row_id=h.order_id,
                record=_record(
                    OrderRecord,
                    h,
                    lines=tuple(
                        _record(OrderLineRecord, line) for line in by_order[h.order_id]
                    ),
                ),
                line_ids={
                    line.line_no: line.order_line_id for line in by_order[h.order_id]
                },
            )
            for h in headers
        }

    async def load_shipments(
        self, company_id: uuid.UUID, shipment_codes: Sequence[str]
    ) -> dict[str, ExistingShipment]:
        headers = (
            await self.db.execute(
                select(
                    Shipment.shipment_id, *_columns(Shipment, _SHIPMENT_COLUMNS)
                ).where(
                    Shipment.company_id == company_id,
                    Shipment.shipment_code.in_(shipment_codes),
                )
            )
        ).all()
        if not headers:
            return {}
        lines = await self.db.execute(
            select(
                ShipmentLine.shipment_id,
                ShipmentLine.shipment_line_id,
                ShipmentLine.line_no,
                *_columns(ShipmentLine, ShipmentLineRecord.FIELDS),
            )
            .where(
                ShipmentLine.company_id == company_id,
                ShipmentLine.shipment_id.in_([h.shipment_id for h in headers]),
            )
            .order_by(ShipmentLine.shipment_id, ShipmentLine.line_no)
        )
        by_shipment: defaultdict[uuid.UUID, list] = defaultdict(list)
        for line in lines:
            by_shipment[line.shipment_id].append(line)
        return {
            h.shipment_code: ExistingShipment(
                row_id=h.shipment_id,
                record=_record(
                    ShipmentRecord,
                    h,
                    lines=tuple(
                        _record(ShipmentLineRecord, line)
                        for line in by_shipment[h.shipment_id]
                    ),
                ),
                line_ids={
                    line.line_no: line.shipment_line_id
                    for line in by_shipment[h.shipment_id]
                },
            )
            for h in headers
        }

    async def load_inventory(
        self,
        company_id: uuid.UUID,
        keys: Sequence[tuple[date, uuid.UUID, uuid.UUID]],
    ) -> dict[tuple[date, uuid.UUID, uuid.UUID], Existing[InventoryRecord]]:
        """키는 (inventory_date, warehouse_id, sku_id). 표시 코드(창고·SKU 코드)도 함께 읽습니다."""
        if not keys:
            return {}
        rows = await self.db.execute(
            select(
                InventoryDaily.inventory_daily_id,
                InventoryDaily.inventory_date,
                InventoryDaily.warehouse_id,
                InventoryDaily.sku_id,
                *_columns(InventoryDaily, InventoryRecord.FIELDS),
                Warehouse.warehouse_code,
                Sku.sku_code,
            )
            .join(
                Warehouse,
                (Warehouse.company_id == InventoryDaily.company_id)
                & (Warehouse.warehouse_id == InventoryDaily.warehouse_id),
            )
            .join(
                Sku,
                (Sku.company_id == InventoryDaily.company_id)
                & (Sku.sku_id == InventoryDaily.sku_id),
            )
            .where(
                InventoryDaily.company_id == company_id,
                tuple_(
                    InventoryDaily.inventory_date,
                    InventoryDaily.warehouse_id,
                    InventoryDaily.sku_id,
                ).in_(list(keys)),
            )
        )
        return {
            (row.inventory_date, row.warehouse_id, row.sku_id): Existing(
                row.inventory_daily_id, _record(InventoryRecord, row)
            )
            for row in rows
        }

    # --- Write: 변경 적용 -----------------------------------------------------------

    async def apply_skus(
        self, company_id: uuid.UUID, changes: Sequence[RowChange[SkuRecord]]
    ) -> None:
        await self._apply(Sku, "sku_id", company_id, changes, _SKU_COLUMNS)

    async def apply_warehouses(
        self, company_id: uuid.UUID, changes: Sequence[RowChange[WarehouseRecord]]
    ) -> None:
        await self._apply(
            Warehouse, "warehouse_id", company_id, changes, _WAREHOUSE_COLUMNS
        )

    async def apply_inventory(
        self,
        company_id: uuid.UUID,
        data_sync_run_id: uuid.UUID,
        changes: Sequence[RowChange[InventoryRecord]],
    ) -> None:
        columns = ("inventory_date", "warehouse_id", "sku_id", *InventoryRecord.FIELDS)
        await self._apply(
            InventoryDaily,
            "inventory_daily_id",
            company_id,
            changes,
            columns,
            extra={"data_sync_run_id": data_sync_run_id},
        )

    async def apply_orders(
        self,
        company_id: uuid.UUID,
        data_sync_run_id: uuid.UUID,
        diffs: Sequence[OrderDiff],
    ) -> None:
        """주문 머리 INSERT·UPDATE 를 먼저, 상세를 다음에 적용합니다. 상세만 바뀐 주문은 수집 ID 만 바꿉니다."""
        run = {"data_sync_run_id": data_sync_run_id}
        await self._apply(
            Order,
            "order_id",
            company_id,
            [d.header for d in diffs if d.header],
            _ORDER_COLUMNS,
            extra=run,
        )
        await self._touch(
            Order,
            "order_id",
            company_id,
            [d.order_id for d in diffs if d.header is None],
            run,
        )
        await self._apply(
            OrderLine,
            "order_line_id",
            company_id,
            [line for d in diffs for line in d.lines],
            ("line_no", *OrderLineRecord.FIELDS),
            parent={
                line.row_id: ("order_id", d.order_id) for d in diffs for line in d.lines
            },
        )

    async def apply_shipments(
        self,
        company_id: uuid.UUID,
        data_sync_run_id: uuid.UUID,
        observed_at: datetime,
        diffs: Sequence[ShipmentDiff],
    ) -> None:
        """선적 머리·상세를 적용하고, 새 선적이거나 단계·예상일이 바뀐 선적은 일정 스냅샷을 남깁니다."""
        run = {"data_sync_run_id": data_sync_run_id}
        await self._apply(
            Shipment,
            "shipment_id",
            company_id,
            [d.header for d in diffs if d.header],
            _SHIPMENT_COLUMNS,
            extra=run,
        )
        await self._touch(
            Shipment,
            "shipment_id",
            company_id,
            [d.shipment_id for d in diffs if d.header is None],
            run,
        )
        await self._apply(
            ShipmentLine,
            "shipment_line_id",
            company_id,
            [line for d in diffs for line in d.lines],
            ("line_no", *ShipmentLineRecord.FIELDS),
            parent={
                line.row_id: ("shipment_id", d.shipment_id)
                for d in diffs
                for line in d.lines
            },
        )
        snapshots = [
            {
                "shipment_schedule_snapshot_id": uuid.uuid7(),
                "company_id": company_id,
                "shipment_id": d.shipment_id,
                "data_sync_run_id": data_sync_run_id,
                "observed_at": observed_at,
                "stage": d.record.current_stage,
                "expected_port_arrival_date": d.record.current_expected_port_arrival_date,
                "expected_3pl_inbound_date": d.record.current_expected_3pl_inbound_date,
            }
            for d in diffs
            if d.schedule_changed
        ]
        if snapshots:
            await self.db.execute(insert(ShipmentScheduleSnapshot), snapshots)

    async def insert_change_records(
        self,
        company_id: uuid.UUID,
        data_sync_run_id: uuid.UUID,
        drafts: Sequence[ChangeRecordDraft],
    ) -> None:
        if not drafts:
            return
        await self.db.execute(
            insert(DataChangeRecord),
            [
                {
                    "data_change_record_id": uuid.uuid7(),
                    "company_id": company_id,
                    "data_sync_run_id": data_sync_run_id,
                    "entity_type": d.entity_type,
                    "entity_ref": d.entity_ref,
                    "change_type": d.change_type,
                    "sku_id": d.sku_id,
                    "warehouse_id": d.warehouse_id,
                    "changed_fields": list(d.changed_fields)
                    if d.changed_fields
                    else None,
                    "change_summary": d.change_summary,
                }
                for d in drafts
            ],
        )

    async def next_applied_seq(self, company_id: uuid.UUID) -> int:
        """고객사의 다음 적용 순번. L1 안에서 불러야 순번이 겹치지 않습니다 (data_sync_runs_uk2 가 최종 방어선)."""
        current = await self.db.scalar(
            select(func.coalesce(func.max(DataSyncRun.applied_seq), 0)).where(
                DataSyncRun.company_id == company_id
            )
        )
        return current + 1

    # --- 수집 실행 기록 ---------------------------------------------------------------

    async def start_run(
        self,
        company_id: uuid.UUID,
        source_domain: SourceDomain,
        connection_method: ConnectionMethod,
        started_at: datetime,
    ) -> uuid.UUID:
        """수집 실행을 RUNNING 으로 남깁니다. 서비스가 적재 트랜잭션과 따로 커밋해 진행 중 상태를 보이게 합니다."""
        run_id = uuid.uuid7()
        await self.db.execute(
            insert(DataSyncRun).values(
                data_sync_run_id=run_id,
                company_id=company_id,
                source_domain=source_domain.value,
                connection_method=connection_method.value,
                started_at=started_at,
                status=DataSyncRunStatus.RUNNING.value,
            )
        )
        return run_id

    async def complete_run(
        self,
        company_id: uuid.UUID,
        data_sync_run_id: uuid.UUID,
        counts: RunCounts,
        applied_seq: int,
        finished_at: datetime,
    ) -> None:
        """적재를 마친 수집: 건수·적용 순번과 함께 SUCCEEDED·PARTIAL_SUCCESS 로 바꿉니다 (ck4~ck8)."""
        await self._finish(
            company_id,
            data_sync_run_id,
            status=counts.status.value,
            finished_at=finished_at,
            received_record_count=counts.received,
            loaded_record_count=counts.loaded,
            rejected_record_count=counts.rejected,
            applied_seq=applied_seq,
        )

    async def fail_run(
        self, company_id: uuid.UUID, data_sync_run_id: uuid.UUID, finished_at: datetime
    ) -> None:
        """적재가 실패한 수집: FAILED. 적용한 것이 없으므로 적용 순번은 비웁니다 (ck8)."""
        await self._finish(
            company_id,
            data_sync_run_id,
            status=DataSyncRunStatus.FAILED.value,
            finished_at=finished_at,
            applied_seq=None,
        )

    async def _finish(
        self, company_id: uuid.UUID, data_sync_run_id: uuid.UUID, **values
    ) -> None:
        """RUNNING 인 실행만 바꿉니다. 이미 끝난 실행을 다시 바꾸려 하면 오류입니다."""
        result = await self.db.execute(
            update(DataSyncRun)
            .where(
                DataSyncRun.company_id == company_id,
                DataSyncRun.data_sync_run_id == data_sync_run_id,
                DataSyncRun.status == DataSyncRunStatus.RUNNING.value,
            )
            .values(values)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise LookupError(f"RUNNING 인 수집 실행이 아닙니다: {data_sync_run_id}")

    # --- 내부 -----------------------------------------------------------------------

    async def _apply(
        self,
        model,
        id_column: str,
        company_id: uuid.UUID,
        changes: Sequence[RowChange],
        columns: Sequence[str],
        *,
        extra: dict | None = None,
        parent: dict[uuid.UUID, tuple[str, uuid.UUID]] | None = None,
    ) -> None:
        """INSERT 는 새 행, UPDATE 는 업무 필드 전체(값이 같은 열은 그대로)와 extra 열을 씁니다."""
        extra = extra or {}
        inserts, updates = [], []
        for change in changes:
            values = {name: getattr(change.record, name) for name in columns} | extra
            if change.change_type == "INSERT":
                if parent:
                    key, value = parent[change.row_id]
                    values[key] = value
                inserts.append(
                    {id_column: change.row_id, "company_id": company_id} | values
                )
            else:
                updates.append(
                    {"b_id": change.row_id}
                    | {f"v_{name}": v for name, v in values.items()}
                )
        if inserts:
            await self.db.execute(insert(model), inserts)
        if updates:
            table = model.__table__
            names = [*columns, *extra]
            statement = (
                update(table)
                .where(table.c.company_id == bindparam("b_company_id"))
                .where(table.c[id_column] == bindparam("b_id"))
                .values({name: bindparam(f"v_{name}") for name in names})
            )
            await self.db.execute(
                statement, [{"b_company_id": company_id, **row} for row in updates]
            )

    async def _touch(
        self,
        model,
        id_column: str,
        company_id: uuid.UUID,
        row_ids: Sequence[uuid.UUID],
        values: dict,
    ) -> None:
        if not row_ids:
            return
        await self.db.execute(
            update(model)
            .where(
                model.company_id == company_id, getattr(model, id_column).in_(row_ids)
            )
            .values(values)
        )


def _columns(model, names: Sequence[str]) -> list:
    return [getattr(model, name) for name in names]


def _record(record_type, row, **overrides):
    """조회 행에서 레코드 타입의 필드만 골라 레코드를 만듭니다."""
    names = record_type.__dataclass_fields__
    values = {
        name: getattr(row, name)
        for name in names
        if name not in overrides and hasattr(row, name)
    }
    return record_type(**values, **overrides)
