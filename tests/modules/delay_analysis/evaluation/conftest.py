import pytest

from app.core.config import get_settings
from app.modules.delay_analysis.infrastructure.engine.engine_loader import (
    load_engine_module,
)
from tests.modules.delay_analysis.engine_test_modules import (
    load_engine_test_module,
)

"""
    평가 시나리오 10건 공통 픽스처 (GLOW-58, GLOW-50 공유 자료 평가시나리오/ — 11-12 까지 이름은 평가시나리오_검토용/)
      - 엔진 묶음의 tests/fixture_glow50.py 로 시나리오 세계(snap)와 엔진 판정(analysis)을 만든다.
      - 엔진 경로(GLOWSHIP_ENGINE_DIR)가 없으면 이 픽스처를 쓰는 시험은 건너뛴다(엔진이 필요 없는 시험 몇 건은 돈다).
      - run_basic·world·analyze 는 파일을 쓰지 않는다. main()(엔진 자체 검사 31건)만 ROOT 아래
        output/ 을 쓰므로 그 시험은 ROOT 를 임시 폴더로 돌린다.
"""


@pytest.fixture(scope="session")
def glow50():
    if get_settings().glowship_engine_dir is None:
        pytest.skip("GLOWSHIP_ENGINE_DIR 이 없어 평가 시나리오 시험을 건너뜀")
    return load_engine_test_module("fixture_glow50")


@pytest.fixture(scope="session")
def scenario(glow50):
    """키 → (snap, replay, analysis). 세션 안에서 한 번만 계산하고 돌려쓴다.
    시험은 돌려받은 값을 고치지 않는다(고쳐서 쓰려면 copy.deepcopy).
      - CFG 키(GS-02·GS-03·GS-07·GS-08·GS-09A·GS-09B·GS-10·GS-11): fixture_glow50.run_basic
      - GS-05: 기존 fixture_gs05 세계 그대로(fixture_glow50.run_all 과 같음)
      - GS-11-probe: 공여 보유 300 → 200 탐침(fixture_glow50.gs11_trace 와 같음)"""
    pipeline = load_engine_module("pipeline")
    gs05 = load_engine_test_module("fixture_gs05")
    cache = {}

    def build(key):
        if key == "GS-05":
            snap, replay = gs05.build_snap()
        elif key == "GS-11-probe":
            snap, replay = glow50.world(
                {**glow50.CFG["GS-11"], "donor": {"on_hand": 200}}
            )
        else:
            return glow50.run_basic(key)
        return snap, replay, pipeline.analyze(snap)

    def run(key):
        if key not in cache:
            cache[key] = build(key)
        return cache[key]

    return run
