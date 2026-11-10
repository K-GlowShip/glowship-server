import asyncio
import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.modules.delay_analysis.infrastructure.engine import engine_input_store

"""
    엔진 저장소 캐시 (이슈 #60, docs/analysis/engine-store-cache.md)
      - 고객사 저장소 하나를 읽는 데 수 초~수십 초가 걸리고(load_company_engine_store), 같은 고객사·같은 입력 순번 실행
        (한 SHIPMENT 수집이 만든 AUTO 실행 여럿, 재분석)은 똑같은 저장소를 읽는다. 그래서 서버 프로세스 메모리에 둔다.
      - 열쇠 = (고객사, 입력 순번). 같은 열쇠라도 나중에 다시 읽으면 바뀌는 것이 두 가지다(문서 2절).
          · 순번 없는 수집 행(RUNNING·FAILED): 판정·도구 출력에 닿지 않는다.
          · 뒤 수집이 적용된 뒤의 현재 상태 표(주문·선적 등)·변경 기록: 저장소가 입력 순번으로 거르지 않는 표다(#36).
            캐시는 먼저 읽은 것을 주므로 그 입력 순번 시점에 더 가깝다. 뒤 수집으로 만든 새 실행은 새 입력 순번이라 새로 읽는다.
      - 같은 열쇠를 동시에 처음 읽으면 열쇠별 asyncio.Lock 으로 한 번만 읽고 나머지는 그 결과를 받는다(로드 합치기).
        이미 있는 열쇠는 잠금 없이 바로 돌려준다. 읽다가 예외·취소되면 넣지 않으므로 다음 호출이 자기 세션으로 다시 읽는다.
      - 항목 수 상한(설정 ENGINE_STORE_CACHE_MAX_ENTRIES)을 넘으면 가장 오래 안 쓴 것부터 버린다. 0 이면 캐시를 끈다.
      - 캐시 자료구조는 이벤트 루프 스레드에서만 다룬다(저장소 읽기·엔진 계산만 스레드). 그래서 스레드 잠금이 필요 없다.
      - 함께 읽는 저장소는 바뀌면 안 된다. 색인 defaultdict 는 저장소가 만들 때 넣지 않는 사전으로 바꾼다(engine_input_store).
      - 서버가 다시 시작되면 비어 있다. load_company_engine_store 는 그대로 두어 캐시 없이 읽는 곳(데모 시드 등)도 그대로 쓴다.
"""

logger = logging.getLogger(__name__)

StoreKey = tuple[uuid.UUID, int]
StoreLoader = Callable[[AsyncSession, uuid.UUID, int], Awaitable[Any]]


@dataclass(frozen=True)
class EngineStoreCacheStats:
    """캐시 사용 횟수. hits = 있던 것을 돌려줌, misses = 새로 읽음, coalesced = 다른 호출이 읽는 것을 기다렸다 받음"""

    hits: int
    misses: int
    coalesced: int
    evictions: int
    size: int


class EngineStoreCache:
    def __init__(self, max_entries: int, loader: StoreLoader | None = None) -> None:
        if max_entries < 0:
            raise ValueError(f"캐시 항목 수 상한은 0 이상이어야 합니다: {max_entries}")
        self._max_entries = max_entries
        # 기본은 부를 때마다 engine_input_store 의 함수를 찾는다(시험이 그 함수를 바꿔 끼울 수 있게)
        self._loader = loader
        self._entries: OrderedDict[StoreKey, Any] = OrderedDict()
        self._locks: dict[StoreKey, asyncio.Lock] = {}
        self._lock_users: dict[StoreKey, int] = {}
        self._hits = self._misses = self._coalesced = self._evictions = 0

    async def _load(
        self, session: AsyncSession, company_id: uuid.UUID, input_seq: int
    ) -> Any:
        loader = self._loader or engine_input_store.load_company_engine_store
        started = time.perf_counter()
        store = await loader(session, company_id, input_seq)
        self._misses += 1
        logger.info(
            "엔진 저장소 읽음: 고객사 %s 입력 순번 %s, %.1f초 (읽기 전 캐시 %d개, 상한 %d)",
            company_id,
            input_seq,
            time.perf_counter() - started,
            len(self._entries),
            self._max_entries,
        )
        return store

    async def get(
        self, session: AsyncSession, company_id: uuid.UUID, input_seq: int
    ) -> Any:
        """(고객사, 입력 순번)의 엔진 저장소. 없으면 session 으로 읽어 넣는다."""
        company_id = uuid.UUID(str(company_id))
        if self._max_entries == 0:
            return await self._load(session, company_id, input_seq)
        key = (company_id, input_seq)
        store = self._entries.get(key)
        if store is not None:
            self._entries.move_to_end(key)
            self._hits += 1
            return store
        lock = self._locks.setdefault(key, asyncio.Lock())
        self._lock_users[key] = self._lock_users.get(key, 0) + 1
        try:
            async with lock:
                store = self._entries.get(key)
                if store is not None:
                    self._entries.move_to_end(key)
                    self._coalesced += 1
                    return store
                store = await self._load(session, company_id, input_seq)
                self._put(key, store)
                return store
        finally:
            self._lock_users[key] -= 1
            if self._lock_users[key] == 0:
                del self._lock_users[key]
                del self._locks[key]

    def _put(self, key: StoreKey, store: Any) -> None:
        self._entries[key] = store
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            evicted, _ = self._entries.popitem(last=False)
            self._evictions += 1
            logger.info("엔진 저장소 캐시에서 버림: 고객사 %s 입력 순번 %s", *evicted)

    def stats(self) -> EngineStoreCacheStats:
        return EngineStoreCacheStats(
            hits=self._hits,
            misses=self._misses,
            coalesced=self._coalesced,
            evictions=self._evictions,
            size=len(self._entries),
        )

    def clear(self) -> None:
        """항목과 횟수를 비운다(시험 사이, 운영 데이터를 수집 밖에서 고쳤을 때). 읽는 중인 호출은 그대로 끝난다."""
        self._entries.clear()
        self._hits = self._misses = self._coalesced = self._evictions = 0


@lru_cache
def engine_store_cache() -> EngineStoreCache:
    """서버 프로세스에 하나. 상한은 처음 부를 때의 설정값"""
    return EngineStoreCache(get_settings().engine_store_cache_max_entries)


async def get_company_engine_store(
    session: AsyncSession, company_id: uuid.UUID, input_seq: int
):
    """캐시를 거친 load_company_engine_store. build_engine_input 이 쓴다."""
    return await engine_store_cache().get(session, company_id, input_seq)
