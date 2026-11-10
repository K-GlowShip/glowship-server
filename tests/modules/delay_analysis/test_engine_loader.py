import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import Settings, get_settings
from app.modules.delay_analysis.infrastructure.engine import engine_loader
from app.modules.delay_analysis.infrastructure.engine.engine_loader import (
    ENGINE_PACKAGE,
    EngineIntegrityError,
    EngineManifestEntry,
    EngineNotConfiguredError,
    EngineTestModuleError,
    import_engine_module,
    load_engine_module,
    read_manifest,
    resolve_engine_dir,
    verify_engine_dir,
)
from tests.modules.delay_analysis import engine_test_modules
from tests.modules.delay_analysis.engine_test_modules import (
    EngineTestFileNotListedError,
    engine_test_file,
    load_engine_test_module,
    read_test_manifest,
)

"""
    데이터 팀 엔진 불러오기 (docs/analysis/engine-local-path.md)
      - 단위 시험: 임시 폴더로 해시 대조·경로 확인·.pyc 미생성을 본다(엔진이 없어도 돎).
      - 해시 목록은 두 개다(이슈 #98). 런타임 목록은 서버가 대조하고, 시험 목록은 시험 쪽 도우미
        (engine_test_modules)가 대조한다. 두 목록의 규칙과 도우미는 가짜 엔진 폴더로 엔진 없이도 확인한다.
      - 실제 엔진 시험: GLOWSHIP_ENGINE_DIR 이 있을 때만 돈다. 없으면 이유를 남기고 건너뛴다(CI·엔진 없는 PC).
"""


REPO_ROOT = Path(__file__).resolve().parents[3]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- 해시 목록 -------------------------------------------------------------------


def _is_engine_test_path(path: str) -> bool:
    return "tests" in path.split("/")[:-1]


def test_runtime_manifest_lists_only_files_the_server_runs():
    manifest = read_manifest()
    paths = {entry.path for entry in manifest}

    assert len(manifest) == 32
    assert len(paths) == 32
    assert "synth2025_v5/allocation_core.py" in paths
    assert "glowship_engine/core.py" in paths
    assert "glowship_engine/episodes.py" in paths
    # 이름과 달리 런타임 파일이다. 엔진 storage.py 가 불러오므로 빼면 저장기를 불러올 수 없다
    assert "glowship_engine/testdb.py" in paths
    # 운영 엔진 묶음에서 시험 폴더를 빼도 서버가 떠야 한다
    assert [path for path in paths if _is_engine_test_path(path)] == []
    assert all(len(entry.sha256) == 64 for entry in manifest)


def test_test_manifest_lists_only_engine_test_files():
    manifest = read_test_manifest()
    paths = {entry.path for entry in manifest}

    assert len(manifest) == 12
    assert len(paths) == 12
    assert [path for path in paths if not _is_engine_test_path(path)] == []
    assert "glowship_engine/tests/test_q9_donor_reason.py" in paths  # Q9 수정판 시험
    # GLOW-50 평가 시나리오 10건 fixture (GLOW-58). 그 fixture 가 불러오는 episodes 는 런타임 목록에 있다
    assert "glowship_engine/tests/fixture_glow50.py" in paths
    assert "synth2025_v5/tests/run_g2.py" in paths
    assert all(len(entry.sha256) == 64 for entry in manifest)


def test_runtime_and_test_manifests_do_not_overlap():
    runtime = {entry.path for entry in read_manifest()}
    tests = {entry.path for entry in read_test_manifest()}

    assert runtime & tests == set()
    assert len(runtime | tests) == 44  # 나누기 전 목록의 파일 수


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


# --- 런타임 목록과 시험 목록 (가짜 엔진 폴더, 엔진 없이 돎) ---------------------------

FAKE_RUNTIME_FILES = {
    "glowship_engine/__init__.py": b"",
    "glowship_engine/core.py": b"VALUE = 1\n",
    "glowship_engine/testdb.py": b"NAME = 'runtime'\n",
    "synth2025_v5/allocation_core.py": b"X = 2\n",
}
FAKE_TEST_FILES = {
    "glowship_engine/tests/__init__.py": b"",
    "glowship_engine/tests/fixture_x.py": (
        b"from glowship_engine import core\n\nVALUE = core.VALUE + 1\n"
    ),
    "synth2025_v5/tests/run_x.py": b"print('x')\n",
}


def _engine_modules() -> dict:
    return {k: v for k, v in sys.modules.items() if k.split(".")[0] == ENGINE_PACKAGE}


@pytest.fixture
def fake_engine(tmp_path, monkeypatch):
    """런타임 파일 4개 + 시험 파일 3개인 가짜 엔진 폴더. 설정과 두 목록을 이 폴더에 맞추고,
    대조 캐시·sys.modules·sys.path 를 시험 앞뒤로 되돌린다(뒤 시험이 가짜 엔진을 보지 않게)."""
    for rel, data in {**FAKE_RUNTIME_FILES, **FAKE_TEST_FILES}.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    runtime = [EngineManifestEntry(r, _sha(d)) for r, d in FAKE_RUNTIME_FILES.items()]
    tests = [EngineManifestEntry(r, _sha(d)) for r, d in FAKE_TEST_FILES.items()]
    settings = SimpleNamespace(glowship_engine_dir=tmp_path)
    monkeypatch.setattr(engine_loader, "get_settings", lambda: settings)
    monkeypatch.setattr(engine_loader, "read_manifest", lambda: runtime)
    monkeypatch.setattr(engine_test_modules, "read_test_manifest", lambda: tests)

    engine_loader.load_verified_engine_dir.cache_clear()
    engine_test_modules.verified_engine_test_dir.cache_clear()
    saved = _engine_modules()
    for name in saved:
        del sys.modules[name]
    try:
        yield tmp_path.resolve()
    finally:
        for name in _engine_modules():
            del sys.modules[name]
        sys.modules.update(saved)
        if str(tmp_path.resolve()) in sys.path:
            sys.path.remove(str(tmp_path.resolve()))
        engine_loader.load_verified_engine_dir.cache_clear()
        engine_test_modules.verified_engine_test_dir.cache_clear()


def test_engine_test_module_loads_after_both_manifests_match(fake_engine):
    module = load_engine_test_module("fixture_x")

    assert module.__name__ == "glowship_engine.tests.fixture_x"
    assert module.VALUE == 2
    assert engine_test_file("synth2025_v5/tests/run_x.py") == (
        fake_engine / "synth2025_v5" / "tests" / "run_x.py"
    )
    assert list(fake_engine.rglob("*.pyc")) == []


def test_runtime_check_and_modules_work_without_engine_test_files(fake_engine):
    """운영 묶음처럼 시험 폴더가 없어도 런타임 대조와 런타임 모듈 불러오기는 된다. 시험 쪽 도우미만 멈춘다."""
    shutil.rmtree(fake_engine / "glowship_engine" / "tests")
    shutil.rmtree(fake_engine / "synth2025_v5" / "tests")

    assert engine_loader.load_verified_engine_dir() == fake_engine
    assert load_engine_module("core").VALUE == 1
    # tests.* 가 아니므로 거부하지 않는다
    assert load_engine_module("testdb").NAME == "runtime"

    with pytest.raises(EngineIntegrityError) as error:
        load_engine_test_module("fixture_x")
    assert error.value.missing == list(FAKE_TEST_FILES)
    assert error.value.mismatched == []


@pytest.mark.parametrize("damage", ["changed", "missing"])
def test_changed_or_missing_engine_test_file_stops_every_test_file(fake_engine, damage):
    """시험 파일은 서로 불러오므로 하나만 어긋나도 시험 목록의 어떤 파일도 내주지 않는다."""
    target = fake_engine / "glowship_engine" / "tests" / "fixture_x.py"
    if damage == "changed":
        target.write_bytes(b"VALUE = 999\n")
    else:
        target.unlink()

    with pytest.raises(EngineIntegrityError) as by_module:
        load_engine_test_module("fixture_x")
    with pytest.raises(EngineIntegrityError):
        engine_test_file("synth2025_v5/tests/run_x.py")

    error = by_module.value
    found = error.mismatched if damage == "changed" else error.missing
    assert found == ["glowship_engine/tests/fixture_x.py"]


def test_engine_test_helper_checks_runtime_manifest_first(fake_engine):
    (fake_engine / "glowship_engine" / "core.py").write_bytes(b"VALUE = 7\n")

    with pytest.raises(EngineIntegrityError) as error:
        load_engine_test_module("fixture_x")

    assert error.value.mismatched == ["glowship_engine/core.py"]


def test_engine_test_helper_refuses_files_outside_test_manifest(fake_engine):
    # 폴더에 있어도 시험 목록에 없으면 대조되지 않은 파일이다
    extra = fake_engine / "glowship_engine" / "tests" / "not_listed.py"
    extra.write_bytes(b"A = 1\n")

    with pytest.raises(EngineTestFileNotListedError, match="not_listed.py"):
        load_engine_test_module("not_listed")
    with pytest.raises(EngineTestFileNotListedError):
        engine_test_file("glowship_engine/core.py")  # 런타임 목록의 파일
    assert "glowship_engine.tests.not_listed" not in sys.modules


def test_engine_test_helper_verifies_once_per_process(fake_engine, monkeypatch):
    calls = []
    verify = engine_test_modules.verify_engine_dir

    def counting(engine_dir, manifest):
        calls.append(len(manifest))
        verify(engine_dir, manifest)

    monkeypatch.setattr(engine_test_modules, "verify_engine_dir", counting)

    load_engine_test_module("fixture_x")
    load_engine_test_module("fixture_x")
    engine_test_file("synth2025_v5/tests/run_x.py")

    assert calls == [len(FAKE_TEST_FILES)]


@pytest.mark.parametrize("name", ["tests", "tests.fixture_gs05", "tests.test_explain"])
def test_runtime_loader_rejects_engine_test_modules(name, monkeypatch):
    """런타임 목록에 없는 파일이라 대조되지 않는다. 대조·불러오기까지 가지 않고 도우미를 안내한다."""
    monkeypatch.setattr(
        engine_loader,
        "load_verified_engine_dir",
        lambda: pytest.fail("거부하기 전에 엔진 폴더를 보면 안 됨"),
    )

    with pytest.raises(EngineTestModuleError, match="load_engine_test_module"):
        load_engine_module(name)


# --- 실제 엔진 (GLOWSHIP_ENGINE_DIR 이 있을 때만) -----------------------------------


def _real_engine_dir() -> Path:
    configured = get_settings().glowship_engine_dir
    if configured is None:
        pytest.skip("GLOWSHIP_ENGINE_DIR 이 없어 실제 엔진 시험을 건너뜀")
    return resolve_engine_dir(configured)


def test_real_engine_dir_matches_manifest():
    verify_engine_dir(_real_engine_dir(), read_manifest())


def test_real_engine_test_files_match_test_manifest():
    verify_engine_dir(_real_engine_dir(), read_test_manifest())


@pytest.mark.parametrize(
    "module",
    [
        "test_engine",
        "test_explain",
        "test_forbidden_boundary",
        "test_q9_donor_reason",
    ],
)
def test_real_engine_self_tests_pass(module):
    _real_engine_dir()  # 엔진 경로가 없으면 건너뜀

    result = load_engine_test_module(module).main()

    ok = result["ok"] if isinstance(result, dict) else result
    assert ok is True


def test_real_allocation_core_g2_passes(tmp_path):
    _real_engine_dir()  # 엔진 경로가 없으면 건너뜀
    runner = engine_test_file("synth2025_v5/tests/run_g2.py")
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


# --- 엔진 시험 폴더를 뺀 묶음 (이슈 #98) ----------------------------------------------

_STARTUP_THEN_IMPORT = """
import json, sys
from app.modules.delay_analysis.infrastructure.engine.engine_loader import (
    load_allocation_module,
    load_engine_module,
)
from app.modules.delay_analysis.infrastructure.engine.engine_startup import (
    verify_engine_on_startup,
)

assert verify_engine_on_startup() is True
names = json.loads(sys.argv[1])
for name in names:
    load_engine_module(name)
load_allocation_module()
print(json.dumps({"imported": len(names)}))
"""


def test_real_engine_without_test_folders_starts_and_imports_runtime_modules(tmp_path):
    """운영 묶음처럼 엔진 tests/ 폴더를 뺀 복사본으로, 깨끗한 프로세스에서 서버 시작 확인이 통과하고
    런타임 엔진 모듈을 모두 불러올 수 있다."""
    source = _real_engine_dir()
    bundle = tmp_path / "bundle"
    modules = []
    for entry in read_manifest():
        if "/tests/" in entry.path:
            continue
        target = bundle / entry.path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / entry.path, target)
        package, _, rest = entry.path.partition("/")
        if package == ENGINE_PACKAGE and rest.endswith(".py"):
            name = rest.removesuffix(".py").replace("/", ".")
            if name != "__init__" and not name.endswith(".__init__"):
                modules.append(name)
    assert not list(bundle.rglob("tests"))

    done = subprocess.run(
        [sys.executable, "-B", "-c", _STARTUP_THEN_IMPORT, json.dumps(modules)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
        cwd=REPO_ROOT,
        # PYTHONUTF8: Windows 에서 자식 프로세스의 한글 오류 문구가 cp949 로 나와 읽다가 깨지지 않게
        env={**os.environ, "GLOWSHIP_ENGINE_DIR": str(bundle), "PYTHONUTF8": "1"},
    )

    assert done.returncode == 0, done.stderr[-2000:]
    assert json.loads(done.stdout.strip().splitlines()[-1]) == {"imported": 20}
