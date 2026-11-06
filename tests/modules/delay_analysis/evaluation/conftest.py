import pytest

from app.core.config import get_settings
from app.modules.delay_analysis.infrastructure.engine.engine_loader import (
    load_engine_module,
)

"""
    평가 시나리오 10건 공통 픽스처 (GLOW-58, Data/평가시나리오_검토용)
      - 엔진 묶음의 tests/fixture_glow50.py 로 시나리오 세계(snap)와 엔진 판정(analysis)을 만든다.
      - 엔진 경로(GLOWSHIP_ENGINE_DIR)가 없으면 이 폴더의 시험은 모두 건너뛴다.
      - run_basic 은 파일을 쓰지 않는다. main()(엔진 자체 검사 31건)만 ROOT 아래 output/ 을 쓰므로
        그 시험은 ROOT 를 임시 폴더로 돌린다.
"""


@pytest.fixture(scope="session")
def glow50():
    if get_settings().glowship_engine_dir is None:
        pytest.skip("GLOWSHIP_ENGINE_DIR 이 없어 평가 시나리오 시험을 건너뜀")
    return load_engine_module("tests.fixture_glow50")


@pytest.fixture(scope="session")
def scenario(glow50):
    """CFG 키(GS-02·GS-03·…) → (snap, replay, analysis). 세션 안에서 한 번만 계산하고 돌려쓴다.
    시험은 돌려받은 값을 고치지 않는다(고쳐서 쓰려면 copy.deepcopy)."""
    cache = {}

    def run(key):
        if key not in cache:
            cache[key] = glow50.run_basic(key)
        return cache[key]

    return run
