import pytest

from app.modules.delay_analysis.infrastructure.engine.engine_store_cache import (
    engine_store_cache,
)


@pytest.fixture(autouse=True)
def _empty_engine_store_cache():
    """엔진 저장소 캐시(이슈 #60)는 프로세스에 하나라, 시험마다 비워 한 시험의 저장소·횟수가 다른 시험에 남지 않게 한다."""
    if engine_store_cache.cache_info().currsize:
        engine_store_cache().clear()
    yield
    if engine_store_cache.cache_info().currsize:
        engine_store_cache().clear()
