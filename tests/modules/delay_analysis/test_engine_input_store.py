import hashlib
import inspect
import os
import shutil
import subprocess
import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.modules.delay_analysis.infrastructure.engine import engine_input_store as EIS
from app.modules.delay_analysis.infrastructure.engine.engine_loader import (
    load_engine_module,
    load_verified_engine_dir,
)

"""
    엔진 입력 저장소 (docs/analysis/engine-input-store.md)
      - 단위 시험: 엔진·DB 없이 돎(CSV 해석·초 아래 버림·오류 감싸기, 시각 변환, 입력 순번 확인, 표 목록·SQL 모양)
      - 엔진 시험: GLOWSHIP_ENGINE_DIR 만 있으면 돎(엔진 Store 소스 해시 고정, 순번 없는 수집을 뺀 input_seq)
      - DB 시험: 엔진 경로 + 개발 DB(overlay 적재). 한 고객사·한 입력 순번만 읽히는지, 리뷰에서 나온 상황
        (마이크로초 시각·RUNNING 수집·겹친 수집)을 롤백 트랜잭션에 넣어 재현
      - 정답 대조: GLOWSHIP_ENGINE_GOLDEN=1 이고 docker 로 DB 컨테이너에 psql 을 쓸 수 있을 때만(몇 분 걸림).
        엔진 원래 방식(psql COPY 로 전체 읽기)으로 만든 Store 와 속성·실행별 스냅샷을 대조한다
"""

# 엔진 store.py 의 Store 클래스·TABLES·DCR_SQL·iso_ts·bool_text·TS_COLS·BOOL_COLS 소스 해시(엔진 manifest 판, 11-05).
# 서버는 Store.__init__ 의 색인 코드를 옮겨 적었고 input_seq 를 덮어썼다(engine_input_store.company_store_class).
# 이 값이 바뀌면: 옮겨 적은 코드를 엔진 새 판과 다시 맞추고, GLOWSHIP_ENGINE_GOLDEN=1 정답 대조를 돌린 뒤 이 값을 갱신한다.
ENGINE_STORE_SOURCE_SHA256 = (
    "8234f5f7b4c2113c0836205bc3d89f1394eed5257d86150beae75f2cc63ea5ed"
)


def _strict_iso(v):
    """엔진 iso_ts 처럼 1초 미만이면 거부하는 대역."""
    if v and "." in v:
        raise ValueError(f"1초 미만 시각 미지원: {v!r}")
    return f"ISO({v})" if v else v


def _stub_store_module(tables=EIS.ENGINE_TABLES):
    def bool_text(v):
        if v in ("", "t", "f"):
            return {"t": "true", "f": "false"}.get(v, v)
        raise ValueError(f"불리언 형식 불가: {v!r}")

    return SimpleNamespace(
        TABLES=list(tables),
        TS_COLS={"started_at"},
        BOOL_COLS={"blocks_analysis"},
        iso_ts=_strict_iso,
        bool_text=bool_text,
    )


# --- 단위 (엔진·DB 없이) -------------------------------------------------------------


def test_parse_engine_csv_converts_only_time_and_bool_columns():
    data = b"id,started_at,blocks_analysis,note\n1,2026-01-02 00:00:00+00,t,\n2,,f,x\n"

    rows = EIS.parse_engine_csv(_stub_store_module(), data)

    assert rows == [
        {
            "id": "1",
            "started_at": "ISO(2026-01-02 00:00:00+00)",
            "blocks_analysis": "true",
            "note": "",
        },
        {"id": "2", "started_at": "", "blocks_analysis": "false", "note": "x"},
    ]


def test_parse_engine_csv_drops_sub_seconds_before_engine_conversion():
    # PR #36 리뷰 [critical]: 서버 수집 시각(datetime.now)에는 마이크로초가 있고 엔진 iso_ts 는 거부한다
    data = b"id,started_at\n1,2026-07-01 00:10:00.123456+00\n2,2026-07-01T00:10:00.5Z\n"

    rows = EIS.parse_engine_csv(_stub_store_module(), data)

    assert [r["started_at"] for r in rows] == [
        "ISO(2026-07-01 00:10:00+00)",
        "ISO(2026-07-01T00:10:00Z)",
    ]


def test_parse_engine_csv_wraps_bad_values_as_engine_input_error():
    data = b"id,blocks_analysis\n1,maybe\n"

    with pytest.raises(EIS.EngineInputError, match="blocks_analysis"):
        EIS.parse_engine_csv(_stub_store_module(), data)


def test_floor_seconds():
    assert (
        EIS.floor_seconds("2026-07-01 00:10:00.123456+00") == "2026-07-01 00:10:00+00"
    )
    assert EIS.floor_seconds("2026-07-01 00:10:00+00") == "2026-07-01 00:10:00+00"
    assert EIS.floor_seconds("") == ""


def test_parse_engine_csv_empty_result():
    assert EIS.parse_engine_csv(_stub_store_module(), b"") == []


def test_engine_timestamp_is_utc_seconds():
    kst = timezone(timedelta(hours=9))

    assert (
        EIS.engine_timestamp(datetime(2026, 1, 2, 9, 0, 5, 123456, tzinfo=kst))
        == "2026-01-02T00:00:05Z"
    )
    assert (
        EIS.engine_timestamp(datetime(2026, 1, 2, 0, 0, 5, tzinfo=UTC))
        == "2026-01-02T00:00:05Z"
    )
    with pytest.raises(EIS.EngineInputError):
        EIS.engine_timestamp(datetime(2026, 1, 2))  # 시간대 없음


def test_check_input_seq_raises_when_engine_view_differs():
    store = SimpleNamespace(input_seq=lambda company_id, t: 7)
    company_id = uuid.uuid4()

    EIS.check_input_seq(store, company_id, "2026-01-02T00:00:00Z", 7)
    with pytest.raises(EIS.EngineInputError, match="입력 순번"):
        EIS.check_input_seq(store, company_id, "2026-01-02T00:00:00Z", 8)


def test_store_module_rejects_engine_with_different_tables(monkeypatch):
    changed = (*EIS.ENGINE_TABLES, "new_table")
    monkeypatch.setattr(
        EIS, "load_engine_module", lambda name: _stub_store_module(changed)
    )

    with pytest.raises(EIS.EngineInputError, match="표 목록"):
        EIS._store_module()


def test_table_queries_filter_by_input_seq_only_where_rows_belong_to_a_run():
    runs, runs_seq = EIS._table_query("data_sync_runs")
    inventory, inventory_seq = EIS._table_query("inventory_daily")
    orders, orders_seq = EIS._table_query("orders")

    assert runs_seq and "applied_seq IS NULL OR applied_seq <= $2" in runs
    assert (
        inventory_seq
        and "data_sync_run_id IN (" in inventory
        and "applied_seq <= $2" in inventory
    )
    assert not orders_seq and "$2" not in orders  # 현재 상태 표는 거르지 않음
    assert EIS.RUN_SCOPED_TABLES == {
        "inventory_daily",
        "shipment_schedule_snapshots",
        "data_validation_issues",
    }
    assert all(q.endswith("ORDER BY ctid") for q in (runs, inventory, orders))


# --- 실제 엔진 (DB 없이) ------------------------------------------------------------


@pytest.fixture
def real_engine():
    if get_settings().glowship_engine_dir is None:
        pytest.skip("GLOWSHIP_ENGINE_DIR 이 없어 실제 엔진 시험을 건너뜀")
    return SimpleNamespace(
        store=load_engine_module("store"),
        snapshot=load_engine_module("snapshot"),
        calendar=_calendar(),
    )


def _calendar():
    path = (
        load_verified_engine_dir()
        / "glowship_engine"
        / "config"
        / "event_calendar_2026h1_v1.json"
    )
    calendar_config = load_engine_module("calendar_config")
    return calendar_config.load_calendar(
        path, hashlib.sha256(path.read_bytes()).hexdigest()
    )


def test_engine_store_source_is_the_version_we_copied(real_engine):
    # PR #36 리뷰 [should]: 옮겨 적은 색인 코드가 엔진 새 판과 조용히 어긋나지 않게, 평소 pytest 에서 잡는다
    s = real_engine.store
    parts = [
        inspect.getsource(s.Store),
        repr(list(s.TABLES)),
        s.DCR_SQL,
        inspect.getsource(s.iso_ts),
        inspect.getsource(s.bool_text),
        repr(sorted(s.TS_COLS)),
        repr(sorted(s.BOOL_COLS)),
    ]
    actual = hashlib.sha256("\n".join(parts).encode()).hexdigest()

    assert actual == ENGINE_STORE_SOURCE_SHA256, (
        "엔진 Store 소스가 바뀌었습니다. engine_input_store.company_store_class 의 옮겨 적은 __init__·input_seq 를"
        " 새 판과 맞추고, GLOWSHIP_ENGINE_GOLDEN=1 정답 대조를 돌린 뒤 ENGINE_STORE_SOURCE_SHA256 을 갱신하세요"
    )


def _empty_tables(**rows):
    tables = {name: [] for name in EIS.ENGINE_TABLES}
    tables.update(rows)
    return tables


def test_company_store_input_seq_ignores_runs_without_applied_seq(real_engine):
    company = str(uuid.uuid4())
    run = lambda seq, at: {"data_sync_run_id": str(uuid.uuid4()), "company_id": company, "started_at": at,
                           "applied_seq": seq, "source_domain": "ORDER", "status": "SUCCEEDED" if seq else "RUNNING"}  # fmt: skip
    store = EIS.company_store_class()(
        uuid.UUID(company),
        _empty_tables(
            data_sync_runs=[
                run("5", "2026-01-01T00:00:00Z"),
                run("", "2026-01-01T00:05:00Z"),
            ]
        ),
        [],
    )

    assert (
        store.input_seq(company, "2026-01-01T00:10:00Z") == 5
    )  # 엔진 원본은 여기서 int('') 로 멈춤
    with pytest.raises(EIS.EngineInputError, match="적재된 수집이 없습니다"):
        store.input_seq(company, "2025-12-31T00:00:00Z")


# --- 실제 엔진 + 개발 DB -----------------------------------------------------------


_RUNS_SQL = text(
    "SELECT r.run_code, r.status, r.started_at, r.input_seq, e.shipment_id,"
    " sn.stage, sn.expected_3pl_inbound_date"
    " FROM analysis_runs r JOIN delay_events e USING (delay_event_id)"
    " LEFT JOIN shipment_schedule_snapshots sn"
    "   ON sn.shipment_schedule_snapshot_id = r.trigger_shipment_schedule_snapshot_id"
    " WHERE r.company_id = :company_id ORDER BY r.started_at, r.run_code"
)


async def _company(session, code: str) -> uuid.UUID:
    company_id = await session.scalar(
        text("SELECT company_id FROM companies WHERE company_code = :c"), {"c": code}
    )
    if company_id is None:
        pytest.skip(f"고객사 {code} 가 없는 DB(overlay 미적재)라 건너뜀")
    return company_id


async def _max_seq(session, company_id) -> int:
    return await session.scalar(
        text("SELECT max(applied_seq) FROM data_sync_runs WHERE company_id = :c"),
        {"c": company_id},
    )


def _build(engine, store, run):
    """snapshot.build 결과 또는 보류 코드(입력 검증 실패는 엔진이 Hold 로 알림)."""
    confirm_t = EIS.engine_timestamp(run.started_at)
    expected = run.expected_3pl_inbound_date
    try:
        return engine.snapshot.build(
            store,
            engine.calendar,
            str(run.shipment_id),
            date.fromisoformat(confirm_t[:10]),
            confirm_t,
            stage=run.stage,
            E=expected.isoformat() if expected else None,
        )
    except engine.snapshot.Hold as hold:
        return ("HOLD", hold.code)


async def test_company_store_reads_only_that_company_up_to_input_seq(
    db_session, real_engine
):
    company_id = await _company(db_session, "B_COSMETICS")
    max_seq = await _max_seq(db_session, company_id)
    cut = max_seq - 300  # 중간 순번으로 잘라 거르기가 실제로 행을 빼는지 본다

    tables, dcr = await EIS.read_company_tables(db_session, company_id, cut)

    params = {"c": company_id, "n": cut}
    for name in EIS.ENGINE_TABLES:
        where = "company_id = :c"
        if name == "data_sync_runs":
            where += " AND (applied_seq IS NULL OR applied_seq <= :n)"
        elif name in EIS.RUN_SCOPED_TABLES:
            where += " AND data_sync_run_id IN (SELECT data_sync_run_id FROM data_sync_runs WHERE company_id = :c AND applied_seq <= :n)"
        count = await db_session.scalar(
            text(f"SELECT count(*) FROM {name} WHERE {where}"), params
        )  # 표 이름은 허용 목록
        assert len(tables[name]) == count, name
        assert {row["company_id"] for row in tables[name]} <= {str(company_id)}, name
    assert (
        max(int(r["applied_seq"]) for r in tables["data_sync_runs"] if r["applied_seq"])
        == cut
    )
    total_inventory = await db_session.scalar(
        text("SELECT count(*) FROM inventory_daily WHERE company_id = :c"), params
    )
    assert (
        len(tables["inventory_daily"]) < total_inventory
    )  # 순번 뒤 수집의 재고는 빠짐
    assert dcr and {row["company_id"] for row in dcr} == {str(company_id)}


async def test_company_store_matches_run_input_seq_and_builds_snapshot(
    db_session, real_engine
):
    company_id = await _company(db_session, "B_COSMETICS")
    runs = (await db_session.execute(_RUNS_SQL, {"company_id": company_id})).all()
    no_risk = next(run for run in runs if run.status == "NO_RISK")

    store = await EIS.load_company_engine_store(
        db_session, company_id, no_risk.input_seq
    )

    EIS.check_input_seq(
        store, company_id, EIS.engine_timestamp(no_risk.started_at), no_risk.input_seq
    )
    snap = _build(real_engine, store, no_risk)
    assert snap["company_id"] == str(company_id)
    assert snap["input_seq"] == no_risk.input_seq


_INSERT_RUN = text(
    "INSERT INTO data_sync_runs (data_sync_run_id, company_id, source_domain, connection_method, started_at,"
    " finished_at, status, received_record_count, loaded_record_count, rejected_record_count, applied_seq)"
    " VALUES (:id, :c, 'ORDER', 'API', :started, :finished, :status, :n, :n, 0, :seq)"
)


async def _insert_run(session, company_id, started, status, seq):
    done = status != "RUNNING"
    await session.execute(
        _INSERT_RUN,
        {
            "id": uuid.uuid4(),
            "c": company_id,
            "started": started,
            "finished": started + timedelta(seconds=30) if done else None,
            "status": status,
            "n": 0 if done else None,
            "seq": seq,
        },
    )


async def test_running_run_with_microseconds_does_not_break_store(
    db_write_session, real_engine
):
    # PR #36 리뷰 [critical] 1·2-①: 서버 수집은 RUNNING 을 먼저 커밋하고 시각에 마이크로초가 있다(롤백 트랜잭션)
    company_id = await _company(db_write_session, "B_COSMETICS")
    run = next(
        r
        for r in (
            await db_write_session.execute(_RUNS_SQL, {"company_id": company_id})
        ).all()
        if r.status == "NO_RISK"
    )
    await _insert_run(
        db_write_session,
        company_id,
        run.started_at - timedelta(minutes=5, microseconds=-123456),
        "RUNNING",
        None,
    )

    store = await EIS.load_company_engine_store(
        db_write_session, company_id, run.input_seq
    )

    running = [r for r in store.t["data_sync_runs"] if r["status"] == "RUNNING"]
    assert len(running) == 1 and running[0]["started_at"].endswith(
        ":00Z"
    )  # 초 아래를 버린 엔진 시각
    EIS.check_input_seq(
        store, company_id, EIS.engine_timestamp(run.started_at), run.input_seq
    )


async def test_run_committed_after_input_is_excluded_even_if_it_started_earlier(
    db_write_session, real_engine
):
    # PR #36 리뷰 [critical] 2-②: 먼저 시작해 L1 을 기다린 수집이 실행 생성 뒤 다음 순번으로 커밋된 경우(롤백 트랜잭션)
    company_id = await _company(db_write_session, "B_COSMETICS")
    run = next(
        r
        for r in (
            await db_write_session.execute(_RUNS_SQL, {"company_id": company_id})
        ).all()
        if r.status == "NO_RISK"
    )
    late_seq = await _max_seq(db_write_session, company_id) + 1
    await _insert_run(
        db_write_session,
        company_id,
        run.started_at - timedelta(minutes=10, microseconds=-654321),
        "SUCCEEDED",
        late_seq,
    )
    confirm_t = EIS.engine_timestamp(run.started_at)

    store = await EIS.load_company_engine_store(
        db_write_session, company_id, run.input_seq
    )

    assert all(
        int(r["applied_seq"]) <= run.input_seq
        for r in store.t["data_sync_runs"]
        if r["applied_seq"]
    )
    EIS.check_input_seq(
        store, company_id, confirm_t, run.input_seq
    )  # 순번으로 거르므로 통과

    # 음성: 그 수집까지 읽은 저장소에서는 엔진의 시각 기준 순번이 실행 기록과 달라진다(거르지 않으면 생기는 일)
    unfiltered = await EIS.load_company_engine_store(
        db_write_session, company_id, late_seq
    )
    with pytest.raises(EIS.EngineInputError, match="입력 순번이 다릅니다"):
        EIS.check_input_seq(unfiltered, company_id, confirm_t, run.input_seq)


# --- 정답 대조: 엔진 원래 방식으로 만든 Store 와 하나씩 (선택 실행) -----------------------------


def _psql_reader():
    if os.environ.get("GLOWSHIP_ENGINE_GOLDEN") != "1":
        pytest.skip("GLOWSHIP_ENGINE_GOLDEN=1 일 때만 도는 정답 대조(몇 분)라 건너뜀")
    if shutil.which("docker") is None:
        pytest.skip("docker 가 없어 정답 대조를 건너뜀")
    settings = get_settings()
    container = os.environ.get("GLOWSHIP_DB_CONTAINER", "glowship-db")

    def read(store, sql: str):
        # 엔진 store.psql_copy 와 같은 명령(컨테이너 이름과 접속 계정만 이 개발 환경 값)
        cmd = [
            "docker", "exec", "-i", container, "psql", "-U", settings.postgres_user, "-d", settings.postgres_db,
            "-q", "-v", "ON_ERROR_STOP=1", "-c", f"COPY ({sql}) TO STDOUT WITH (FORMAT csv, HEADER)",
        ]  # fmt: skip
        result = subprocess.run(cmd, capture_output=True, check=False)
        if result.returncode:
            pytest.skip(
                f"psql 로 DB 를 읽지 못해 정답 대조를 건너뜀(코드 {result.returncode})"
            )
        return EIS.parse_engine_csv(store, result.stdout)

    return read


def _engine_store_from(store_module, rows_by_sql, monkeypatch):
    """엔진 Store.__init__ 을 그대로 돌리되, psql 읽기만 미리 읽은 행으로 바꾼다."""
    monkeypatch.setattr(store_module, "psql_copy", lambda db, sql: rows_by_sql[sql])
    engine_store = store_module.Store("golden")
    monkeypatch.undo()
    return engine_store


@pytest.mark.parametrize("company_code", ["A_BEAUTY", "B_COSMETICS"])
async def test_company_store_equals_engine_store_golden(
    db_session, real_engine, monkeypatch, company_code
):
    read = _psql_reader()
    company_id = await _company(db_session, company_code)
    store_module = real_engine.store
    sqls = [f"SELECT * FROM {name}" for name in store_module.TABLES] + [
        store_module.DCR_SQL
    ]
    rows_by_sql = {
        sql: [r for r in read(store_module, sql) if r["company_id"] == str(company_id)]
        for sql in sqls
    }
    engine_store = _engine_store_from(store_module, rows_by_sql, monkeypatch)

    # 1) 마지막 순번까지 읽으면(overlay 는 모든 수집이 완료) 엔진이 직접 만든 Store 와 속성이 행 순서까지 같다
    full = await EIS.load_company_engine_store(
        db_session, company_id, await _max_seq(db_session, company_id)
    )
    expected, actual = vars(engine_store), vars(full)
    assert set(expected) == set(actual)
    assert [k for k in expected if k != "db" and expected[k] != actual[k]] == []

    # 2) 실행마다 그 실행의 입력 순번으로 읽은 저장소의 스냅샷 = 엔진(전체, 시각 기준)의 스냅샷
    runs = (await db_session.execute(_RUNS_SQL, {"company_id": company_id})).all()
    by_seq = {}
    for run in runs:
        if run.input_seq not in by_seq:
            by_seq[run.input_seq] = await EIS.load_company_engine_store(
                db_session, company_id, run.input_seq
            )
        ours = by_seq[run.input_seq]
        EIS.check_input_seq(
            ours, company_id, EIS.engine_timestamp(run.started_at), run.input_seq
        )
        assert _build(real_engine, ours, run) == _build(
            real_engine, engine_store, run
        ), run.run_code

    # 음성: 값 하나만 달라도 대조가 잡는다
    tampered = {k: list(v) for k, v in full.t.items()}
    tampered["skus"][0] = {**tampered["skus"][0], "sku_code": "TAMPERED"}
    assert tampered != engine_store.t
