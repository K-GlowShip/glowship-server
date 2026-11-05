import hashlib
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
      - 단위 시험: 엔진·DB 없이 돎(CSV 해석 규칙, 시각 변환, 입력 순번 확인, 표 목록 검사)
      - DB 시험: GLOWSHIP_ENGINE_DIR 과 개발 DB(overlay 적재)가 있을 때. 한 고객사만 읽히는지, 행 수, 입력 순번, 스냅샷
      - 정답 대조: GLOWSHIP_ENGINE_GOLDEN=1 이고 docker 로 DB 컨테이너에 psql 을 쓸 수 있을 때만(1분 이상 걸림).
        엔진 원래 방식(psql COPY 로 전체 읽기)으로 만든 Store 와 속성·스냅샷을 하나씩 대조한다
"""


def _stub_store_module(tables=EIS.ENGINE_TABLES):
    def iso_ts(v):
        return f"ISO({v})" if v else v

    def bool_text(v):
        return {"t": "true", "f": "false"}.get(v, v)

    return SimpleNamespace(
        TABLES=list(tables),
        TS_COLS={"started_at"},
        BOOL_COLS={"blocks_analysis"},
        iso_ts=iso_ts,
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


# --- 실제 엔진 + 개발 DB -----------------------------------------------------------


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


_RUNS_SQL = text(
    "SELECT r.run_code, r.status, r.started_at, r.input_seq, e.shipment_id,"
    " sn.stage, sn.expected_3pl_inbound_date"
    " FROM analysis_runs r JOIN delay_events e USING (delay_event_id)"
    " LEFT JOIN shipment_schedule_snapshots sn"
    "   ON sn.shipment_schedule_snapshot_id = r.trigger_shipment_schedule_snapshot_id"
    " WHERE r.company_id = :company_id ORDER BY r.started_at, r.run_code"
)


async def _company(db_session, code: str) -> uuid.UUID:
    company_id = await db_session.scalar(
        text("SELECT company_id FROM companies WHERE company_code = :c"), {"c": code}
    )
    if company_id is None:
        pytest.skip(f"고객사 {code} 가 없는 DB(overlay 미적재)라 건너뜀")
    return company_id


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


async def test_company_store_reads_only_that_company(db_session, real_engine):
    company_id = await _company(db_session, "B_COSMETICS")

    tables, dcr = await EIS.read_company_tables(db_session, company_id)

    for name in EIS.ENGINE_TABLES:
        count = await db_session.scalar(
            text(
                f"SELECT count(*) FROM {name} WHERE company_id = :c"
            ),  # 표 이름은 허용 목록
            {"c": company_id},
        )
        assert len(tables[name]) == count, name
        assert {row["company_id"] for row in tables[name]} <= {str(company_id)}, name
    assert dcr and {row["company_id"] for row in dcr} == {str(company_id)}


async def test_company_store_matches_run_input_seq_and_builds_snapshot(
    db_session, real_engine
):
    company_id = await _company(db_session, "B_COSMETICS")
    store = await EIS.load_company_engine_store(db_session, company_id)
    runs = (await db_session.execute(_RUNS_SQL, {"company_id": company_id})).all()
    assert runs, "overlay 실행이 없습니다"

    for run in runs:  # 엔진의 "수집 시작 ≤ 입력 확정 시각" 기준 = 실행의 input_seq
        EIS.check_input_seq(
            store, company_id, EIS.engine_timestamp(run.started_at), run.input_seq
        )

    no_risk = next(run for run in runs if run.status == "NO_RISK")
    snap = _build(real_engine, store, no_risk)
    assert snap["company_id"] == str(company_id)
    assert snap["input_seq"] == no_risk.input_seq


# --- 정답 대조: 엔진 원래 방식으로 만든 Store 와 하나씩 (선택 실행) -----------------------------


def _psql_reader():
    if os.environ.get("GLOWSHIP_ENGINE_GOLDEN") != "1":
        pytest.skip(
            "GLOWSHIP_ENGINE_GOLDEN=1 일 때만 도는 정답 대조(1분 이상)라 건너뜀"
        )
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

    ours = await EIS.load_company_engine_store(db_session, company_id)

    expected, actual = vars(engine_store), vars(ours)
    assert set(expected) == set(actual)
    assert [
        k for k in expected if k != "db" and expected[k] != actual[k]
    ] == []  # 행 순서까지 같음

    runs = (await db_session.execute(_RUNS_SQL, {"company_id": company_id})).all()
    for run in runs:
        assert _build(real_engine, ours, run) == _build(
            real_engine, engine_store, run
        ), run.run_code

    # 음성: 값 하나만 달라도 대조가 잡는다
    tampered = {k: list(v) for k, v in ours.t.items()}
    tampered["skus"][0] = {**tampered["skus"][0], "sku_code": "TAMPERED"}
    assert tampered != engine_store.t
