import csv
import io
import uuid
from datetime import UTC, datetime
from functools import lru_cache
from types import ModuleType

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rls_context import set_rls_context
from app.modules.delay_analysis.infrastructure.engine.engine_loader import (
    load_engine_module,
)

"""
    엔진 입력 저장소 (GLOW-125, docs/analysis/engine-input-store.md)
      - 데이터 팀 엔진의 입력 스냅샷(snapshot.build)은 업무 DB를 메모리에 올린 Store 를 받습니다.
        엔진 Store 는 `docker exec psql` 로 DB 전체(모든 고객사)를 읽으므로 서버에서는 쓸 수 없습니다.
      - 여기서는 서버 DB 연결로 **한 고객사만** 같은 표·같은 모양으로 읽어 엔진 Store 와 똑같은 객체를 만듭니다.
        엔진 코드(스냅샷·as-of 조회)는 고치지 않고 그대로 씁니다.
      - 읽는 방법도 엔진과 같게 `COPY (...) TO STDOUT CSV` 입니다. 그래서 시각·숫자·불리언·JSON 의 글자 모양이
        엔진이 psql 로 받는 것과 바이트까지 같고, 엔진의 변환 함수(iso_ts·bool_text)를 그대로 씁니다.
      - 고객사 범위: RLS 컨텍스트(app.company_id)를 설정하고, 조회 조건에도 company_id 를 넣습니다
        (RLS 가 우회되는 로컬 superuser 접속에서도 다른 고객사 행은 읽히지 않음, run_status_repository 와 같은 방식).
      - 입력 순번: 엔진은 "수집 시작 시각 ≤ 입력 확정 시각"으로 그때 알려진 자료를 고릅니다. 이 기준이 실행의
        input_seq 와 같은지 check_input_seq 로 확인하고, 다르면 멈춥니다(엔진 결과와 실행 기록이 갈라지지 않게).
      - 읽기 전용입니다. 커밋하지 않습니다.
"""

# 엔진 store.TABLES 와 같아야 하는 표 목록. 엔진 판이 바뀌어 표가 늘거나 줄면 불러올 때 멈춘다(SQL 에 넣는 이름의 허용 목록).
ENGINE_TABLES = (
    "companies",
    "warehouses",
    "skus",
    "operating_rule_sets",
    "operating_lead_times",
    "data_sync_runs",
    "data_validation_issues",
    "external_entity_mappings",
    "orders",
    "order_lines",
    "shipments",
    "shipment_lines",
    "shipment_schedule_snapshots",
    "inventory_daily",
)


class EngineInputError(Exception):
    """엔진 입력을 만들 수 없음(엔진 판과 서버 코드가 어긋남, 입력 순번 불일치 등)."""


def _store_module() -> ModuleType:
    store = load_engine_module("store")
    if tuple(store.TABLES) != ENGINE_TABLES:
        raise EngineInputError(
            f"엔진 Store 의 표 목록이 서버와 다릅니다: {store.TABLES}. 엔진 새 판이면 이 파일의 ENGINE_TABLES 와 시험을 함께 바꾸세요"
        )
    return store


def parse_engine_csv(store: ModuleType, data: bytes) -> list[dict[str, str]]:
    """엔진 store.psql_copy 와 같은 해석: CSV 머리 줄 → dict, 시각 열은 iso_ts, 불리언 열은 bool_text."""
    rows = list(csv.DictReader(io.StringIO(data.decode("utf-8"))))
    if rows:
        ts_cols = [c for c in rows[0] if c in store.TS_COLS]
        bool_cols = [c for c in rows[0] if c in store.BOOL_COLS]
        for row in rows:
            for c in ts_cols:
                row[c] = store.iso_ts(row[c])
            for c in bool_cols:
                row[c] = store.bool_text(row[c])
    return rows


async def _copy_csv(session: AsyncSession, query: str, company_id: uuid.UUID) -> bytes:
    """현재 트랜잭션(RLS 컨텍스트가 걸린 연결)에서 COPY (query) TO STDOUT CSV HEADER 결과를 받는다."""
    connection = await session.connection()
    raw = await connection.get_raw_connection()
    chunks: list[bytes] = []

    async def sink(chunk: bytes) -> None:
        chunks.append(bytes(chunk))

    await raw.driver_connection.copy_from_query(
        query, str(company_id), output=sink, format="csv", header=True
    )
    return b"".join(chunks)


@lru_cache
def company_store_class() -> type:
    """엔진 Store 를 상속해 '읽기'만 바꾼 클래스. as-of 조회 메서드는 엔진 것을 그대로 쓴다.

    __init__ 의 색인 만들기는 엔진 store.Store.__init__(엔진 판 manifest 고정)와 같은 순서·같은 식이다.
    엔진 판이 바뀌면 시험(test_engine_input_store)이 엔진이 직접 만든 Store 와 속성을 하나씩 대조해 어긋남을 잡는다.
    """
    store = _store_module()
    d = load_engine_module("common").d
    from collections import defaultdict
    from datetime import timedelta

    class CompanyEngineStore(store.Store):
        def __init__(
            self, company_id: uuid.UUID, t: dict[str, list[dict]], dcr: list[dict]
        ):
            self.db = f"server:{company_id}"
            self.t = t
            self.company = {r["company_code"]: r for r in t["companies"]}
            self.company_code = {
                r["company_id"]: r["company_code"] for r in t["companies"]
            }
            self.wh = {r["warehouse_id"]: r for r in t["warehouses"]}
            self.wh_by_code = {
                (r["company_id"], r["warehouse_code"]): r for r in t["warehouses"]
            }
            self.sku = {r["sku_id"]: r for r in t["skus"]}
            self.runs = {r["data_sync_run_id"]: r for r in t["data_sync_runs"]}
            self.run_at = {
                (r["company_id"], r["started_at"]): r for r in t["data_sync_runs"]
            }
            self.orders = {r["order_id"]: r for r in t["orders"]}
            self.lines = defaultdict(list)
            for line in t["order_lines"]:
                self.lines[line["order_id"]].append(line)
            self.ships = {r["shipment_id"]: r for r in t["shipments"]}
            self.ship_lines = defaultdict(list)
            for line in t["shipment_lines"]:
                self.ship_lines[line["shipment_id"]].append(line)
            self.snaps = defaultdict(list)
            for s in t["shipment_schedule_snapshots"]:
                self.snaps[s["shipment_id"]].append(s)
            for v in self.snaps.values():
                v.sort(key=lambda x: x["observed_at"])
            self.inv = {
                (
                    r["company_id"],
                    r["warehouse_id"],
                    r["sku_id"],
                    r["inventory_date"],
                ): r
                for r in t["inventory_daily"]
            }
            self.inv_by_id = {r["inventory_daily_id"]: r for r in t["inventory_daily"]}
            self.dcr = dcr
            self.dcr_by_day = defaultdict(list)
            for c in self.dcr:
                self.dcr_by_day[(c["company_id"], d(c["started_at"]))].append(c)
            self.ship_line_owner = {
                line["shipment_line_id"]: line["shipment_id"]
                for line in t["shipment_lines"]
            }
            # 주문 출고 수집일(첫 ORDER_LINE UPDATE): 출고일 = 수집일 − 1
            line_order = {
                line["order_line_id"]: line["order_id"] for line in t["order_lines"]
            }
            first_ship = {}
            for c in self.dcr:
                if c["entity_type"] == "ORDER_LINE" and c["change_type"] == "UPDATE":
                    oid = line_order.get(c["entity_ref"])
                    cd = d(c["started_at"])
                    if oid and (oid not in first_ship or cd < first_ship[oid]):
                        first_ship[oid] = cd
            self.dispatch_date = {
                oid: cd - timedelta(1) for oid, cd in first_ship.items()
            }
            # 수요 집계(유효 주문, 접수일·창고·SKU·채널)
            self.demand = defaultdict(int)
            self.demand_ch = defaultdict(int)
            for o in t["orders"]:
                if o["status"] in ("CANCELLED", "REMOVED_AT_SOURCE"):
                    continue
                od = o["ordered_at"][:10]
                for line in self.lines[o["order_id"]]:
                    if line["line_status"] == "ACTIVE":
                        q = int(line["ordered_quantity"])
                        self.demand[(o["warehouse_id"], line["sku_id"], od)] += q
                        self.demand_ch[
                            (o["warehouse_id"], line["sku_id"], od, o["sales_channel"])
                        ] += q

    return CompanyEngineStore


async def read_company_tables(
    session: AsyncSession, company_id: uuid.UUID
) -> tuple[dict[str, list[dict[str, str]]], list[dict[str, str]]]:
    """한 고객사의 엔진 입력 표 14개와 변경 기록(엔진 DCR_SQL 범위)을 엔진 모양으로 읽는다."""
    store = _store_module()
    await set_rls_context(session, user_id=None, company_id=company_id)
    tables = {}
    for name in ENGINE_TABLES:
        # ORDER BY ctid: 엔진은 조건 없이 전체를 읽어 물리 순서로 받는다. 고객사 조건을 걸면 색인 순서로 올 수 있어 순서를 맞춘다
        query = f"SELECT * FROM {name} WHERE company_id = $1 ORDER BY ctid"  # 표 이름은 허용 목록(ENGINE_TABLES)
        tables[name] = parse_engine_csv(
            store, await _copy_csv(session, query, company_id)
        )
    dcr_data = await _copy_csv(
        session, f"{store.DCR_SQL} AND d.company_id = $1", company_id
    )
    return tables, parse_engine_csv(store, dcr_data)


async def load_company_engine_store(session: AsyncSession, company_id: uuid.UUID):
    """한 고객사 범위의 엔진 Store. snapshot.build(store, …)에 그대로 넘깁니다."""
    tables, dcr = await read_company_tables(session, company_id)
    return company_store_class()(company_id, tables, dcr)


def engine_timestamp(value: datetime) -> str:
    """서버 시각 → 엔진 시각 문자열(UTC 'YYYY-MM-DDTHH:MM:SSZ'). 엔진은 1초 미만을 다루지 않으므로 초 아래는 버린다."""
    if value.tzinfo is None:
        raise EngineInputError(
            f"시간대가 없는 시각은 엔진 입력으로 쓸 수 없습니다: {value!r}"
        )
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_input_seq(
    store, company_id: uuid.UUID, confirm_t: str, input_seq: int
) -> None:
    """엔진이 confirm_t 기준으로 보는 입력 순번이 실행의 input_seq 와 같은지 확인한다. 다르면 EngineInputError."""
    engine_seq = store.input_seq(str(company_id), confirm_t)
    if engine_seq != input_seq:
        raise EngineInputError(
            f"입력 순번이 다릅니다: 엔진(수집 시작 ≤ {confirm_t}) {engine_seq}, 실행 input_seq {input_seq}."
            " 입력 확정 뒤에 시작 시각이 앞선 수집이 커밋됐거나 시각 기록이 어긋났습니다"
        )
