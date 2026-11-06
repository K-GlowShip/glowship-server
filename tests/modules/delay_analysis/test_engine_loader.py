import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.core.config import Settings, get_settings
from app.modules.delay_analysis.infrastructure.engine.engine_loader import (
    ENGINE_PACKAGE,
    EngineIntegrityError,
    EngineManifestEntry,
    EngineNotConfiguredError,
    import_engine_module,
    read_manifest,
    resolve_engine_dir,
    verify_engine_dir,
)

"""
    데이터 팀 엔진 불러오기 (docs/analysis/engine-local-path.md)
      - 단위 시험: 임시 폴더로 해시 대조·경로 확인·.pyc 미생성을 본다(엔진이 없어도 돎).
      - 실제 엔진 시험: GLOWSHIP_ENGINE_DIR 이 있을 때만 돈다. 없으면 이유를 남기고 건너뛴다(CI·엔진 없는 PC).
"""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- 해시 목록 -------------------------------------------------------------------


def test_manifest_lists_engine_and_allocation_core_files():
    manifest = read_manifest()
    paths = {entry.path for entry in manifest}

    assert len(manifest) == 42
    assert len(paths) == 42
    assert "synth2025_v5/allocation_core.py" in paths
    assert "glowship_engine/core.py" in paths
    assert "glowship_engine/tests/test_q9_donor_reason.py" in paths  # Q9 수정판 시험
    assert all(len(entry.sha256) == 64 for entry in manifest)


# --- 해시 대조 -------------------------------------------------------------------


def _fake_engine(tmp_path: Path) -> tuple[Path, list[EngineManifestEntry]]:
    files = {
        "glowship_engine/__init__.py": b"",
        "glowship_engine/core.py": b"VALUE = 1\n",
        "synth2025_v5/allocation_core.py": b"X = 2\n",
    }
    for rel, data in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return tmp_path, [
        EngineManifestEntry(rel, _sha(data)) for rel, data in files.items()
    ]


def test_verify_passes_when_all_listed_files_match(tmp_path):
    engine_dir, manifest = _fake_engine(tmp_path)
    (engine_dir / "glowship_engine" / "extra_tool.py").write_text("# 목록에 없는 파일")

    verify_engine_dir(engine_dir, manifest)


def test_verify_reports_changed_and_missing_files(tmp_path):
    engine_dir, manifest = _fake_engine(tmp_path)
    (engine_dir / "glowship_engine" / "core.py").write_bytes(
        b"VALUE = 1\r\n"
    )  # 줄바꿈만 바뀜
    (engine_dir / "synth2025_v5" / "allocation_core.py").unlink()

    with pytest.raises(EngineIntegrityError) as error:
        verify_engine_dir(engine_dir, manifest)

    assert error.value.mismatched == ["glowship_engine/core.py"]
    assert error.value.missing == ["synth2025_v5/allocation_core.py"]


def test_resolve_requires_configured_dir_with_engine_package(tmp_path):
    with pytest.raises(EngineNotConfiguredError):
        resolve_engine_dir(None)
    with pytest.raises(EngineNotConfiguredError):
        resolve_engine_dir(tmp_path)  # glowship_engine 폴더 없음

    (tmp_path / ENGINE_PACKAGE).mkdir()
    assert resolve_engine_dir(tmp_path) == tmp_path.resolve()


def test_resolve_turns_relative_and_home_paths_into_absolute(tmp_path, monkeypatch):
    # 상대경로·~ 가 그대로 sys.path 에 들어가면 실행 폴더에 따라 다른 엔진을 볼 수 있다(PR #25 리뷰)
    (tmp_path / "engine_root" / ENGINE_PACKAGE).mkdir(parents=True)
    expected = (tmp_path / "engine_root").resolve()

    monkeypatch.chdir(tmp_path)
    relative = resolve_engine_dir(Path("engine_root"))

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows 의 expanduser 기준
    home = resolve_engine_dir(Path("~") / "engine_root")

    assert relative == expected and relative.is_absolute()
    assert home == expected and home.is_absolute()


@pytest.mark.parametrize("raw", ["", "   "])
def test_blank_engine_dir_setting_means_not_configured(monkeypatch, raw):
    # `GLOWSHIP_ENGINE_DIR=` 로 비워 두면 Path("") 가 "." 이 되어 설정된 것처럼 보였다(11-05 발견)
    monkeypatch.setenv("GLOWSHIP_ENGINE_DIR", raw)
    settings = Settings(
        _env_file=None,
        postgres_host="h",
        postgres_port=5432,
        postgres_user="u",
        postgres_password="p",
        postgres_db="d",
        jwt_secret_key="x" * 32,
    )

    assert settings.glowship_engine_dir is None


def test_import_does_not_write_bytecode_into_engine_dir(tmp_path):
    engine_dir, _ = _fake_engine(tmp_path)
    saved = {k: v for k, v in sys.modules.items() if k.split(".")[0] == ENGINE_PACKAGE}
    for name in saved:
        del sys.modules[name]
    try:
        module = import_engine_module(engine_dir, "core")
        assert module.VALUE == 1
        assert list(engine_dir.rglob("*.pyc")) == []
    finally:
        for name in [k for k in sys.modules if k.split(".")[0] == ENGINE_PACKAGE]:
            del sys.modules[name]
        sys.modules.update(saved)
        if str(engine_dir) in sys.path:
            sys.path.remove(str(engine_dir))


# --- 실제 엔진 (GLOWSHIP_ENGINE_DIR 이 있을 때만) -----------------------------------


def _real_engine_dir() -> Path:
    configured = get_settings().glowship_engine_dir
    if configured is None:
        pytest.skip("GLOWSHIP_ENGINE_DIR 이 없어 실제 엔진 시험을 건너뜀")
    return resolve_engine_dir(configured)


def test_real_engine_dir_matches_manifest():
    verify_engine_dir(_real_engine_dir(), read_manifest())


@pytest.mark.parametrize(
    "module",
    [
        "tests.test_engine",
        "tests.test_explain",
        "tests.test_forbidden_boundary",
        "tests.test_q9_donor_reason",
    ],
)
def test_real_engine_self_tests_pass(module):
    engine_dir = _real_engine_dir()
    verify_engine_dir(engine_dir, read_manifest())

    result = import_engine_module(engine_dir, module).main()

    ok = result["ok"] if isinstance(result, dict) else result
    assert ok is True


def test_real_allocation_core_g2_passes(tmp_path):
    engine_dir = _real_engine_dir()
    runner = engine_dir / "synth2025_v5" / "tests" / "run_g2.py"
    pre = tmp_path / "pre.json"
    pre.write_text(json.dumps({}), encoding="utf-8")

    done = subprocess.run(
        [sys.executable, "-I", "-B", str(runner), str(pre), str(tmp_path / "out")],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=600,
    )

    assert done.returncode == 0, done.stderr
    summary = json.loads(done.stdout.strip().splitlines()[-1])
    assert summary["g2"] == "PASS"
