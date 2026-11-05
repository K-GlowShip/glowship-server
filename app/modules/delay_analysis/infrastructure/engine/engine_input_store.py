import asyncio
import csv
import io
import re
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
      - 입력 순번(PR #36 리뷰 반영): 서버는 수집 시작 시각을 L1 전에 정하므로(ingest_service), 겹친 수집에서는
        "시작 시각 순서"와 "적용 순번 순서"가 다릅니다. 그래서 **읽을 때 실행의 input_seq 로 거릅니다.**
          · 적용 순번 > input_seq 인 수집과, 그 수집이 만든 재고·일정 스냅샷·검증 이슈는 읽지 않는다
          · 변경 기록은 거르지 않는다(엔진이 나중 기록으로 현재 상태를 그 시점으로 되돌려 보므로, 거르면 과거 복원이 틀어짐)
          · 순번이 없는 RUNNING·FAILED 수집은 엔진처럼 시각으로 판단하게 둔다(그날 수집이 시작됐는지 보는 규칙)
          · 주문·선적 같은 현재 상태 표는 거르지 않는다(data_sync_run_id 가 "마지막으로 고친 수집"이라). 엔진의 시각 규칙에 맡긴다
          · input_seq 는 적재된(순번 있는) 수집만 본다(엔진 원본은 순번 없는 행에서 int('') 로 멈춤)
        그래서 저장소 한 개 = (고객사, 입력 순번) 하나입니다. check_input_seq 는 그 위에서 마지막 확인을 합니다.
      - 시각: 서버 수집은 마이크로초가 있고 엔진 iso_ts 는 1초 미만을 거부하므로, 읽을 때 초 아래를 버립니다(engine_timestamp 와 같은 규칙).
      - CSV 해석과 색인 만들기는 무거우므로(한 고객사 수 초) 이벤트 루프를 막지 않게 스레드에서 합니다.
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


# PostgreSQL 시각 출력의 1초 미만 부분('2026-07-01 00:10:00.123456+00' 의 '.123456')
_SUB_SECOND = re.compile(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})\.\d+")


def floor_seconds(value: str) -> str:
    """시각 문자열의 1초 미만을 버린다. 엔진 iso_ts 는 1초 미만을 거부하고, 서버 수집 시각에는 마이크로초가 있다."""
    return _SUB_SECOND.sub(r"\1", value) if value else value


def parse_engine_csv(store: ModuleType, data: bytes) -> list[dict[str, str]]:
    """엔진 store.psql_copy 와 같은 해석: CSV 머리 줄 → dict, 시각 열은 (초 아래 버림 →) iso_ts, 불리언 열은 bool_text.

    해석할 수 없는 값은 EngineInputError(어느 열·어떤 값인지 포함)로 알린다.
    """
    rows = list(csv.DictReader(io.StringIO(data.decode("utf-8"))))
    if rows:
        ts_cols = [c for c in rows[0] if c in store.TS_COLS]
        bool_cols = [c for c in rows[0] if c in store.BOOL_COLS]
        for row in rows:
            column = None
            try:
                for column in ts_cols:
                    row[column] = store.iso_ts(floor_seconds(row[column]))
                for column in bool_cols:
                    row[column] = store.bool_text(row[column])
            except ValueError as error:
                raise EngineInputError(
                    f"엔진 입력 값을 해석할 수 없습니다({column}={row.get(column)!r}): {error}"
                ) from error
    return rows


async def _copy_csv(session: AsyncSession, query: str, *args: object) -> bytes:
    """현재 트랜잭션(RLS 컨텍스트가 걸린 연결)에서 COPY (query) TO STDOUT CSV HEADER 결과를 받는다."""
    connection = await session.connection()
    raw = await connection.get_raw_connection()
    chunks: list[bytes] = []

    async def sink(chunk: bytes) -> None:
        chunks.append(bytes(chunk))

    await raw.driver_connection.copy_from_query(
        query, *args, output=sink, format="csv", header=True
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

        def input_seq(self, company_id, t):
            """엔진 Store.input_seq 와 같되 적재된(순번 있는) 수집만 본다. RUNNING·FAILED 는 applied_seq 가 비어 있다."""
            seqs = [
                int(r["applied_seq"])
                for r in self.runs_until(company_id, t)
                if r["applied_seq"]
            ]
            if not seqs:
                raise EngineInputError(
                    f"입력 확정 시각 {t} 까지 적재된 수집이 없습니다(고객사 {company_id})"
                )
            return max(seqs)

    return CompanyEngineStore


# 수집이 만든 행이라 그 수집의 적용 순번으로 거르는 표. 주문·선적은 "마지막으로 고친 수집"을 가리켜 거르지 않는다
RUN_SCOPED_TABLES = frozenset(
    {"inventory_daily", "shipment_schedule_snapshots", "data_validation_issues"}
)
_APPLIED_RUNS = "SELECT data_sync_run_id FROM data_sync_runs WHERE company_id = $1 AND applied_seq <= $2"


def _table_query(name: str) -> tuple[str, bool]:
    """(SQL, 입력 순번을 쓰는지). 표 이름은 허용 목록(ENGINE_TABLES)에서만 온다.

    ORDER BY ctid: 엔진은 조건 없이 전체를 읽어 물리 순서로 받는다. 조건을 걸면 색인 순서로 올 수 있어 순서를 맞춘다.
    """
    if name == "data_sync_runs":
        return (
            (
                "SELECT * FROM data_sync_runs WHERE company_id = $1"
                " AND (applied_seq IS NULL OR applied_seq <= $2) ORDER BY ctid"
            ),
            True,
        )
    if name in RUN_SCOPED_TABLES:
        return (
            f"SELECT * FROM {name} WHERE company_id = $1 AND data_sync_run_id IN ({_APPLIED_RUNS}) ORDER BY ctid",
            True,
        )
    return f"SELECT * FROM {name} WHERE company_id = $1 ORDER BY ctid", False


async def read_company_tables(
    session: AsyncSession, company_id: uuid.UUID, input_seq: int
) -> tuple[dict[str, list[dict[str, str]]], list[dict[str, str]]]:
    """한 고객사·한 입력 순번의 엔진 입력 표 14개와 변경 기록(엔진 DCR_SQL 범위)을 엔진 모양으로 읽는다."""
    store = _store_module()
    await set_rls_context(session, user_id=None, company_id=company_id)
    args = (str(company_id), input_seq)
    tables = {}
    for name in ENGINE_TABLES:
        query, uses_seq = _table_query(name)
        data = await _copy_csv(session, query, *(args if uses_seq else args[:1]))
        tables[name] = await asyncio.to_thread(parse_engine_csv, store, data)
    # 변경 기록은 순번으로 거르지 않는다. 엔진은 나중 수집의 변경 기록(출고 기록 등)으로 주문의 현재 상태를
    # 그 시점의 상태로 되돌려 본다(store.dispatch_date → order_open_at_close). 거르면 출고된 주문이 처음부터 출고된 것으로 보여
    # 예약 복원이 DB 와 달라진다(11-05 정답 대조에서 B_COSMETICS NO_RISK 실행이 REQUIRED_DATA_INVALID 로 바뀜).
    dcr_data = await _copy_csv(
        session, f"{store.DCR_SQL} AND d.company_id = $1", args[0]
    )
    return tables, await asyncio.to_thread(parse_engine_csv, store, dcr_data)


async def load_company_engine_store(
    session: AsyncSession, company_id: uuid.UUID, input_seq: int
):
    """한 고객사·한 입력 순번(실행의 input_seq) 범위의 엔진 Store. snapshot.build(store, …)에 그대로 넘깁니다."""
    tables, dcr = await read_company_tables(session, company_id, input_seq)
    return await asyncio.to_thread(company_store_class(), company_id, tables, dcr)


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
            " 실행의 입력 순번까지 적재된 수집 중 시작 시각이 입력 확정 시각보다 늦은 것이 있거나, 다른 입력 순번으로 읽은 저장소입니다"
        )
