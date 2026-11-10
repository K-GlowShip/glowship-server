from functools import lru_cache
from pathlib import Path
from types import ModuleType

from app.modules.delay_analysis.infrastructure.engine.engine_loader import (
    ENGINE_PACKAGE,
    ENGINE_TEST_PACKAGE,
    EngineManifestEntry,
    import_engine_module,
    load_verified_engine_dir,
    read_manifest,
    verify_engine_dir,
)

"""
    엔진 시험 파일 불러오기 (docs/analysis/engine-local-path.md 3절)
      - 엔진 묶음의 시험 파일(glowship_engine/tests 의 자체 시험·fixture, synth2025_v5/tests 의 판정 실행기)은
        서버가 실행할 때 쓰지 않는다. 그래서 서버 쪽 해시 목록(런타임)에 넣지 않고 이 폴더의 시험 목록
        (engine_test_manifest.json)에 따로 둔다. 운영 엔진 묶음에서 시험 폴더를 빼도 서버가 뜬다(이슈 #98).
      - 시험이 엔진 시험 파일을 쓸 때는 여기 함수로 부른다. 런타임 목록 대조(서버와 같은 load_verified_engine_dir) 뒤
        시험 목록을 대조하고, 시험 목록에 있는 파일만 돌려준다. 대조는 프로세스당 한 번 한다.
      - 서버 쪽 load_engine_module("tests.…")는 EngineTestModuleError 를 낸다(대조하지 않은 파일을 불러오지 않게).
      - 엔진 경로(GLOWSHIP_ENGINE_DIR)가 없으면 EngineNotConfiguredError 가 난다. 건너뛸지는 부르는 시험이 정한다.
"""

TEST_MANIFEST_PATH = Path(__file__).with_name("engine_test_manifest.json")


class EngineTestFileNotListedError(LookupError):
    """시험 목록에 없는 엔진 파일을 달라고 함(대조되지 않은 파일)"""

    def __init__(self, path: str):
        self.path = path
        super().__init__(
            f"{path} 는 엔진 시험 목록({TEST_MANIFEST_PATH.name})에 없습니다."
            " 목록에 있는 파일만 불러옵니다"
        )


def read_test_manifest() -> list[EngineManifestEntry]:
    return read_manifest(TEST_MANIFEST_PATH)


@lru_cache
def verified_engine_test_dir() -> Path:
    """런타임 목록 대조 뒤 시험 목록까지 대조한 엔진 폴더(프로세스당 한 번)."""
    engine_dir = load_verified_engine_dir()
    verify_engine_dir(engine_dir, read_test_manifest())
    return engine_dir


def engine_test_file(path: str) -> Path:
    """예) engine_test_file("synth2025_v5/tests/run_g2.py") — 시험 목록에 있는 파일의 대조된 경로"""
    if path not in {entry.path for entry in read_test_manifest()}:
        raise EngineTestFileNotListedError(path)
    return verified_engine_test_dir() / path


def load_engine_test_module(name: str) -> ModuleType:
    """예) load_engine_test_module("fixture_gs05") → glowship_engine.tests.fixture_gs05"""
    engine_test_file(
        f"{ENGINE_PACKAGE}/{ENGINE_TEST_PACKAGE}/{name.replace('.', '/')}.py"
    )
    return import_engine_module(
        verified_engine_test_dir(), f"{ENGINE_TEST_PACKAGE}.{name}"
    )
