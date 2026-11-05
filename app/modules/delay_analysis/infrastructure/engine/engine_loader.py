import hashlib
import importlib
import json
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import ModuleType

from app.core.config import get_settings

"""
    데이터 팀 분석 엔진 불러오기 (docs/analysis/engine-local-path.md)
      - 엔진 코드(glowship_engine)와 주문 배정 모듈(synth2025_v5/allocation_core)은 이 저장소에 넣지 않는다.
        이 저장소는 공개 저장소이고, 엔진은 데이터 팀 결과물(원본은 비공개 glowship-plan·공유 묶음)이기 때문이다.
      - 설정 GLOWSHIP_ENGINE_DIR 이 가리키는 폴더의 원본을, 저장소에 둔 해시 목록(engine_manifest.json)으로
        한 파일씩 대조한 뒤 불러온다. 하나라도 없거나 다르면 불러오지 않고 멈춘다(판이 어긋난 엔진으로 계산하지 않음).
      - 폴더 모양: GLOWSHIP_ENGINE_DIR/glowship_engine/…, GLOWSHIP_ENGINE_DIR/synth2025_v5/…
        엔진 core.py·snapshot.py 가 형제 폴더 synth2025_v5 를 sys.path 에 넣고 allocation_core 를 import 하기 때문이다.
      - 불러올 때 원본 폴더에 .pyc 캐시 파일을 만들지 않는다(공유 묶음을 바꾸지 않음).
"""

MANIFEST_PATH = Path(__file__).with_name("engine_manifest.json")
ENGINE_PACKAGE = "glowship_engine"


class EngineNotConfiguredError(RuntimeError):
    """GLOWSHIP_ENGINE_DIR 이 비어 있거나 폴더가 없음"""


class EngineIntegrityError(RuntimeError):
    """엔진 폴더의 파일이 해시 목록과 다름(없음·내용 다름)"""

    def __init__(self, missing: list[str], mismatched: list[str]):
        self.missing = missing
        self.mismatched = mismatched
        super().__init__(
            "엔진 원본이 해시 목록과 다릅니다."
            f" 없는 파일 {len(missing)}개 {missing[:5]}, 내용이 다른 파일 {len(mismatched)}개 {mismatched[:5]}."
            " 데이터 팀 원본(공유 묶음 04_적재검증도구 + glowship-plan tools/synth2025_v5)인지 확인하세요"
        )


@dataclass(frozen=True)
class EngineManifestEntry:
    path: str
    sha256: str


def read_manifest(manifest_path: Path = MANIFEST_PATH) -> list[EngineManifestEntry]:
    doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    return [EngineManifestEntry(f["path"], f["sha256"]) for f in doc["files"]]


def verify_engine_dir(engine_dir: Path, manifest: list[EngineManifestEntry]) -> None:
    """목록의 모든 파일이 있고 바이트가 같은지 본다. 목록에 없는 파일(하네스·배포 도구 등)은 보지 않는다."""
    missing, mismatched = [], []
    for entry in manifest:
        path = engine_dir / entry.path
        if not path.is_file():
            missing.append(entry.path)
        elif hashlib.sha256(path.read_bytes()).hexdigest() != entry.sha256:
            mismatched.append(entry.path)
    if missing or mismatched:
        raise EngineIntegrityError(missing, mismatched)


def resolve_engine_dir(configured: Path | None) -> Path:
    if configured is None:
        raise EngineNotConfiguredError(
            "GLOWSHIP_ENGINE_DIR 이 설정되지 않았습니다. 엔진 폴더(glowship_engine·synth2025_v5 의 부모) 경로를 .env 에 넣으세요"
        )
    engine_dir = Path(configured)
    if not (engine_dir / ENGINE_PACKAGE).is_dir():
        raise EngineNotConfiguredError(
            f"GLOWSHIP_ENGINE_DIR({engine_dir}) 아래에 {ENGINE_PACKAGE} 폴더가 없습니다"
        )
    return engine_dir


def import_engine_module(engine_dir: Path, name: str) -> ModuleType:
    """해시를 대조한 엔진 폴더에서 glowship_engine 하위 모듈을 불러온다. 원본 폴더에 .pyc 를 쓰지 않는다."""
    parent = str(engine_dir)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        return importlib.import_module(f"{ENGINE_PACKAGE}.{name}")
    finally:
        sys.dont_write_bytecode = previous


@lru_cache
def load_verified_engine_dir() -> Path:
    """설정의 엔진 폴더를 해시 목록으로 대조하고 경로를 돌려준다(프로세스당 한 번)."""
    engine_dir = resolve_engine_dir(get_settings().glowship_engine_dir)
    verify_engine_dir(engine_dir, read_manifest())
    return engine_dir


def load_engine_module(name: str) -> ModuleType:
    """예) load_engine_module("pipeline"), load_engine_module("explain.validator")"""
    return import_engine_module(load_verified_engine_dir(), name)
