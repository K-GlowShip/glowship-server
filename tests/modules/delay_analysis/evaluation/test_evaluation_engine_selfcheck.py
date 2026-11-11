import contextlib
import io
import shutil
import subprocess
import sys
from datetime import date

import pytest

from app.core.config import get_settings
from app.modules.delay_analysis.application.run_creation.run_code import (
    format_run_code,
)
from app.modules.delay_analysis.infrastructure.engine.engine_loader import (
    load_engine_module,
    load_verified_engine_dir,
)
from tests.modules.delay_analysis.engine_test_modules import (
    load_engine_test_module,
)

"""
    엔진 묶음의 GLOW-50 자체 검사(fixture_glow50.main)를 서버 시험 안에서 돌린다.
      1. 계산 검사 31건(--db 없음): 전부 PASS. 결과 파일은 임시 폴더에만 쓴다(ROOT 를 돌림) — 엔진 묶음 폴더는 그대로.
      2. 저장 검사(--db): 엔진 저장기가 만든 행을 이 세션의 DB(glowship-db_<N>, docker 컨테이너 glowship-db)에
         넣어 CHECK·FK 를 모두 통과하는지 보고 ROLLBACK 한다(52건 전부 PASS, 거부 0, 행 수 그대로).
         - 서버 DB 는 마이그레이션 0006 이 analysis_runs.run_code NOT NULL 을 더했다. 엔진 v6.2 저장기는 이 열을
           모르므로 실행 행마다 run_code(AN-260310-9NN)를 넣어 준다. 그 밖의 서버 변경(0003·0005·0007)은 그대로 통과했다(11-06).
         - docker 가 없거나 DB 에 못 붙으면 건너뛴다. 엔진 testdb 의 컨테이너(glowship-erd-pg)·사용자(postgres)는
           이 환경과 달라 psql 을 바꿔 끼운다. JSON v3 검증기는 엔진 묶음의 04_적재검증도구/ 에서 임시 ROOT 로 복사한다.
      3. 음성: 저장 행 하나를 CHECK 위반 값으로 바꾸면 그 행이 REJECTED 로 잡히고, 그래도 롤백된다.
"""

CONTAINER = "glowship-db"
RUN_CODE_BASIS = date(2026, 3, 10)
RUN_CODE_FIRST_SEQ = 900
COUNT_TABLES = (
    "delay_events",
    "analysis_runs",
    "inventory_impact_results",
    "at_risk_orders",
    "response_options",
    "option_order_outcomes",
)


def _main(glow50, *args):
    """엔진 main() 은 '−' 같은 문자를 찍으므로 콘솔 인코딩과 무관하게 출력을 가둔다."""
    argv = ["fixture_glow50", *args]
    with contextlib.redirect_stdout(io.StringIO()), pytest.MonkeyPatch.context() as mp:
        mp.setattr(sys, "argv", argv)
        return glow50.main()


@pytest.fixture
def engine_root(glow50, monkeypatch, tmp_path):
    """fixture_glow50.ROOT(결과 파일 위치)를 임시 폴더로. 원래 ROOT 는 엔진 묶음 폴더(04_적재검증도구의 부모)다."""
    original = glow50.ROOT
    monkeypatch.setattr(glow50, "ROOT", tmp_path)
    return original


def test_engine_calculation_checks_all_pass(glow50, engine_root, tmp_path):
    result = _main(glow50)
    failed = [c["item"] for c in result["checks"] if not c["ok"]]
    assert failed == []
    assert result["passed"] == result["total"] == 31
    assert "storage" not in result
    written = list(tmp_path.rglob("results.json"))
    assert len(written) == 1 and written[0].is_relative_to(tmp_path / "output")
    assert not (engine_root / "output").exists()  # 엔진 묶음 폴더에는 쓰지 않았다


# --- 저장 검사(DB) --------------------------------------------------------------------------


def _psql_factory(user: str):
    def psql(db, sql=None, stdin=None):
        cmd = [
            "docker", "exec", "-i", CONTAINER, "psql", "-U", user, "-d", db,
            "-At", "-q", "-v", "ON_ERROR_STOP=1", "-F", "\t",
        ]  # fmt: skip
        try:
            r = subprocess.run(
                cmd,
                input=stdin if stdin is not None else sql.encode("utf-8"),
                capture_output=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return 1, "", f"{type(error).__name__}: {error}"
        out = r.stdout.decode("utf-8", "replace").replace("\r\n", "\n").rstrip("\n")
        return r.returncode, out, r.stderr.decode("utf-8", "replace").strip()

    return psql


@pytest.fixture
def storage_db(glow50, monkeypatch, tmp_path, engine_root):
    """이 세션 DB 에 붙는 psql 로 바꿔 끼우고, run_code 를 넣는 rows_for 로 감싼다. 못 붙으면 건너뛴다."""
    if shutil.which("docker") is None:
        pytest.skip("docker 가 없어 엔진 저장 검사를 건너뜀")
    settings = get_settings()
    psql = _psql_factory(settings.postgres_user)
    db = settings.postgres_db
    rc, _out, err = psql(db, "SELECT 1")
    if rc != 0:
        pytest.skip(f"컨테이너 {CONTAINER} 의 DB {db} 에 못 붙어 건너뜀: {err[:120]}")
    taken = psql(
        db,
        "SELECT count(*) FROM analysis_runs WHERE run_code LIKE "
        f"'{format_run_code(RUN_CODE_BASIS, RUN_CODE_FIRST_SEQ)[:-2]}%'",
    )[1]
    if taken != "0":
        pytest.skip("시험용 run_code(AN-260310-9NN)가 이미 DB 에 있어 건너뜀")

    storage = load_engine_module("storage")
    for module in (glow50, load_engine_test_module("fixture_gs05"), storage):
        monkeypatch.setattr(module, "psql", psql)
    original_rows_for = storage.rows_for
    seq = iter(range(RUN_CODE_FIRST_SEQ, 1000))

    def rows_for_with_run_code(history):
        rows = original_rows_for(history)
        for _label, table, row in rows:
            if table == "analysis_runs" and "run_code" not in row:
                row["run_code"] = format_run_code(RUN_CODE_BASIS, next(seq))
        return rows

    monkeypatch.setattr(storage, "rows_for", rows_for_with_run_code)

    validator = load_verified_engine_dir() / "sku_results_v3_validate.py"
    if validator.exists():
        target = tmp_path / "review" / "track_b_policy_analysis"
        target.mkdir(parents=True)
        shutil.copy(validator, target / validator.name)
    else:
        monkeypatch.setattr(
            glow50, "json_v3_check", lambda items: {"rc": None, "skipped": True}
        )

    def counts():
        return {t: psql(db, f"SELECT count(*) FROM {t}")[1] for t in COUNT_TABLES}

    return {"db": db, "psql": psql, "counts": counts, "storage": storage}


def test_engine_storage_rows_pass_db_constraints_and_roll_back(glow50, storage_db):
    before = storage_db["counts"]()
    result = _main(glow50, f"--db={storage_db['db']}")
    after = storage_db["counts"]()
    assert after == before  # 전부 ROLLBACK
    failed = [c["item"] for c in result["checks"] if not c["ok"]]
    assert failed == []
    assert result["passed"] == result["total"] == 52
    for entry in result["storage"]:
        if "not_applicable" in entry:  # GS-04
            continue
        assert entry["rejected"] == [], entry["label"]
        assert entry["rolled_back"] is True, entry["label"]
    if not result["json_v3"].get("skipped"):
        assert result["json_v3"]["rc"] == 0
        assert '"ok": true' in result["json_v3"]["out"]


def test_storage_check_catches_constraint_violation(glow50, storage_db):
    """GS-03 행에서 KR 대응안의 확정 감소를 음수로 바꾸면 response_options CHECK 가 거부한다(음성)."""
    storage, db = storage_db["storage"], storage_db["db"]
    base = glow50.real_ids_single(db)
    ids = glow50.ids_for(glow50.CFG["GS-03"], base)
    snap, _replay, analysis = glow50.run_basic("GS-03", ids)
    event = glow50.ev_of("GS-03", ids, snap["P"], snap["E"], snap["stage"], 903)
    run = glow50.run_of("GS-03", ids, event, snap, analysis)
    good_rows_for = storage.rows_for

    def rows_for_with_bad_option(history):
        rows = good_rows_for(history)
        for _label, table, row in rows:
            if table == "response_options" and row["option_type"] == "KR_AIR_REPLENISH":
                row["reduction_committed_total"] = -1
        return rows

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(storage, "rows_for", rows_for_with_bad_option)
        before = storage_db["counts"]()
        stored = glow50.store_runs(db, "GS-03", event, [run])
        assert storage_db["counts"]() == before
    rejected = {r[1]: r[2] for r in stored["rejected"]}
    # 바꾼 행은 CHECK 위반(23514), 그 행을 참조하는 주문별 결과 1행만 FK 로 따라 거부(23503)
    assert rejected == {
        "ro GS-03 v1 KR_AIR_REPLENISH": "23514",
        "oo GS-03 v1 KR_AIR_REPLENISH O1#1": "23503",
    }
    assert stored["ok"] == stored["rows"] - 2
    assert stored["rolled_back"] is True
