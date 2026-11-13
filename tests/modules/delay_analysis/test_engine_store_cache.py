import asyncio
import hashlib
import pickle
import threading
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import pytest
import test_engine_input as TEI

from app.modules.delay_analysis.infrastructure.engine import engine_input as EI
from app.modules.delay_analysis.infrastructure.engine import engine_input_store as EIS
from app.modules.delay_analysis.infrastructure.engine.engine_store_cache import (
    EngineStoreCache,
    engine_store_cache,
)

"""
    엔진 저장소 캐시와 넣지 않는 색인 (이슈 #60, docs/analysis/engine-store-cache.md)
      - 단위 시험(엔진·DB 없음): 가짜 로더로 같은 열쇠 한 번만 읽기, 동시 로드 합치기, LRU 버리기, 실패·취소, 캐시 끄기,
        NoInsertDefaultDict(없는 열쇠를 읽어도 그대로, 여러 스레드가 읽고 돌아도 오류 없음)
      - DB 시험(엔진 경로 + overlay): 같은 입력 순번 실행 4건(A_BEAUTY 입력 순번 10)이 저장소를 한 번만 읽고,
        그 저장소로 엔진 계산을 여러 스레드에서 동시에 돌려도 결과·저장소가 그대로인지
"""

COMPANY_A, COMPANY_B = uuid.UUID(int=1), uuid.UUID(int=2)
engine_dir = TEI.engine_dir  # 엔진 경로가 없으면 DB 시험을 건너뛰는 고정값


class _FakeLoader:
    """부를 때마다 새 객체를 돌려준다. gate 를 주면 그 이벤트가 설 때까지 기다린다(동시 로드 시험)."""

    def __init__(self, gate: asyncio.Event | None = None, fail: bool = False):
        self.calls: list[tuple[uuid.UUID, int]] = []
        self.gate, self.fail = gate, fail

    async def __call__(self, session, company_id, input_seq):
        self.calls.append((company_id, input_seq))
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise EIS.EngineInputError("읽기 실패(시험)")
        return object()


# --- 캐시 (단위) -----------------------------------------------------------------------------------


async def test_same_key_reads_once_and_other_keys_read_separately():
    loader = _FakeLoader()
    cache = EngineStoreCache(4, loader)

    first = await cache.get(None, COMPANY_A, 10)
    again = await cache.get(None, str(COMPANY_A), 10)  # 문자열 고객사 ID 도 같은 열쇠
    other_seq = await cache.get(None, COMPANY_A, 11)
    other_company = await cache.get(None, COMPANY_B, 10)

    assert again is first
    assert other_seq is not first and other_company is not first
    assert loader.calls == [(COMPANY_A, 10), (COMPANY_A, 11), (COMPANY_B, 10)]
    stats = cache.stats()
    assert (stats.hits, stats.misses, stats.coalesced, stats.size) == (1, 3, 0, 3)


async def test_concurrent_first_reads_of_same_key_load_once():
    # 한 SHIPMENT 수집이 만든 AUTO 실행 4개가 동시에 시작하는 모양(실행기 동시 실행 기본 4)
    gate = asyncio.Event()
    loader = _FakeLoader(gate)
    cache = EngineStoreCache(2, loader)

    tasks = [asyncio.create_task(cache.get(None, COMPANY_A, 10)) for _ in range(4)]
    await asyncio.sleep(0)
    assert loader.calls == [(COMPANY_A, 10)]  # 나머지 셋은 잠금에서 기다림
    gate.set()
    stores = await asyncio.gather(*tasks)

    assert all(s is stores[0] for s in stores)
    assert len(loader.calls) == 1
    stats = cache.stats()
    assert (stats.misses, stats.coalesced, stats.hits) == (1, 3, 0)
    assert cache._locks == {} and cache._lock_users == {}  # 열쇠별 잠금이 남지 않음


async def test_least_recently_used_entry_is_evicted_over_limit():
    loader = _FakeLoader()
    cache = EngineStoreCache(2, loader)

    a10 = await cache.get(None, COMPANY_A, 10)
    await cache.get(None, COMPANY_B, 10)
    assert (
        await cache.get(None, COMPANY_A, 10) is a10
    )  # A 를 다시 써서 B 가 가장 오래 안 쓴 것
    await cache.get(None, COMPANY_A, 11)  # 셋째 열쇠 → B 를 버림

    assert cache.stats().size == 2 and cache.stats().evictions == 1
    assert await cache.get(None, COMPANY_A, 10) is a10
    await cache.get(None, COMPANY_B, 10)  # 버린 것은 다시 읽음
    assert loader.calls.count((COMPANY_B, 10)) == 2


async def test_evict_company_drops_only_that_company_and_next_call_reads_again():
    """수집 없이 저장소 표가 바뀐 고객사(운영 기준 새 버전 저장, GLOW-153)만 버린다"""
    loader = _FakeLoader()
    cache = EngineStoreCache(4, loader)
    a10 = await cache.get(None, COMPANY_A, 10)
    await cache.get(None, COMPANY_A, 11)
    b10 = await cache.get(None, COMPANY_B, 10)

    assert cache.evict_company(str(COMPANY_A)) == 2  # 문자열 고객사 ID 도 같은 열쇠
    assert cache.evict_company(COMPANY_A) == 0

    assert cache.stats().size == 1
    assert await cache.get(None, COMPANY_B, 10) is b10
    assert await cache.get(None, COMPANY_A, 10) is not a10
    assert loader.calls.count((COMPANY_A, 10)) == 2


async def test_failed_load_is_not_cached_and_next_call_reads_again():
    loader = _FakeLoader(fail=True)
    cache = EngineStoreCache(2, loader)

    with pytest.raises(EIS.EngineInputError):
        await cache.get(None, COMPANY_A, 10)
    assert cache.stats().size == 0

    loader.fail = False
    store = await cache.get(None, COMPANY_A, 10)
    assert store is not None and len(loader.calls) == 2
    assert cache._locks == {}


async def test_cancelled_first_reader_lets_waiting_reader_load_with_its_own_session():
    # 서버 종료 유예 때 첫 실행이 저장소를 읽다가 취소돼도, 기다리던 실행은 자기 세션으로 다시 읽는다
    gate = asyncio.Event()
    sessions = []

    async def loader(session, company_id, input_seq):
        sessions.append(session)
        await gate.wait()
        return object()

    cache = EngineStoreCache(2, loader)
    first = asyncio.create_task(cache.get("세션1", COMPANY_A, 10))
    second = asyncio.create_task(cache.get("세션2", COMPANY_A, 10))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    gate.set()
    store = await second

    assert sessions == ["세션1", "세션2"]
    assert await cache.get("세션3", COMPANY_A, 10) is store
    assert cache._locks == {} and cache._lock_users == {}


async def test_zero_limit_turns_cache_off():
    loader = _FakeLoader()
    cache = EngineStoreCache(0, loader)

    first = await cache.get(None, COMPANY_A, 10)
    second = await cache.get(None, COMPANY_A, 10)

    assert first is not second and len(loader.calls) == 2
    stats = cache.stats()
    assert (stats.size, stats.misses, stats.evictions) == (
        0,
        2,
        0,
    )  # 넣었다 버리지 않고 그냥 읽음


def test_negative_limit_is_rejected():
    with pytest.raises(ValueError):
        EngineStoreCache(-1)


def test_process_cache_uses_setting(monkeypatch):
    engine_store_cache.cache_clear()
    monkeypatch.setattr(
        "app.modules.delay_analysis.infrastructure.engine.engine_store_cache.get_settings",
        lambda: type("S", (), {"engine_store_cache_max_entries": 3})(),
    )
    try:
        assert engine_store_cache()._max_entries == 3
    finally:
        engine_store_cache.cache_clear()


# --- 넣지 않는 색인 (단위) ----------------------------------------------------------------------------


def test_no_insert_dict_returns_default_without_inserting():
    counts = EIS.NoInsertDefaultDict(int, {("a",): 1})
    lists = EIS.NoInsertDefaultDict(list, {})

    assert counts[("없음",)] == 0 and len(counts) == 1
    assert lists["없음"] == [] and len(lists) == 0
    lists["없음"].append(1)  # 돌려준 빈 목록은 매번 새것이라 고쳐도 사전에 남지 않음
    assert lists["없음"] == []
    # 내용이 같으면 defaultdict 와 같다(엔진 Store 대조 시험의 전제)
    assert counts == defaultdict(int, {("a",): 1})
    assert pickle.loads(pickle.dumps(counts)) == counts


def _race(index) -> tuple[list[str], int]:
    """세 스레드가 없는 열쇠를 [] 로 읽는 동안 한 스레드가 items() 를 돈다(엔진 snapshot.build 의 mu()·STALE 경로 모양)."""
    errors, stop = [], threading.Event()

    def reader():
        i = 0
        while not stop.is_set():
            index[("없음", i)]
            i += 1

    def walker():
        try:
            for _ in range(200):
                for _ in index.items():
                    pass
        except RuntimeError as error:
            errors.append(str(error))
        finally:
            stop.set()

    threads = [threading.Thread(target=reader) for _ in range(3)]
    threads.append(threading.Thread(target=walker))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors, len(index)


def test_no_insert_dict_is_safe_for_concurrent_readers():
    base = {("k", i): i for i in range(2000)}

    errors, size = _race(EIS.NoInsertDefaultDict(int, base))
    assert (errors, size) == ([], 2000)

    # 음성 대조: defaultdict 는 읽기가 넣기라 크기가 늘어난다(이 시험이 위험을 실제로 잡는지)
    _, grown = _race(defaultdict(int, base))
    assert grown > 2000


def test_freeze_keeps_attribute_names_order_and_contents():
    store = type("S", (), {})()
    for name in EIS.DEFAULT_DICT_INDEXES:
        factory = int if name.startswith("demand") else list
        setattr(store, name, defaultdict(factory, {"k": factory()}))
    before = list(vars(store))

    EIS.freeze_default_dicts(store)

    assert list(vars(store)) == before
    for name in EIS.DEFAULT_DICT_INDEXES:
        index = getattr(store, name)
        assert isinstance(index, EIS.NoInsertDefaultDict) and index == {
            "k": index.default_factory()
        }
        index["없음"]
        assert len(index) == 1


# --- 실제 엔진 + 개발 DB -------------------------------------------------------------------------


def _fingerprint(store) -> tuple[str, dict[str, int]]:
    sizes = {name: len(getattr(store, name)) for name in EIS.DEFAULT_DICT_INDEXES}
    return hashlib.sha256(pickle.dumps(vars(store), protocol=5)).hexdigest(), sizes


async def _shared_seq_runs(session):
    runs = [r for r in await TEI._runs(session, "A_BEAUTY") if r.input_seq == 10]
    if len(runs) != 4:
        pytest.skip(
            f"A_BEAUTY 입력 순번 10 실행이 4건이 아닌 DB 라 건너뜀({len(runs)}건)"
        )
    return runs


async def test_runs_with_same_input_seq_read_store_once(
    db_session, engine_dir, monkeypatch
):
    calls = []
    original = EIS.load_company_engine_store

    async def counting(session, company_id, input_seq):
        calls.append((company_id, input_seq))
        return await original(session, company_id, input_seq)

    monkeypatch.setattr(EIS, "load_company_engine_store", counting)
    runs = await _shared_seq_runs(db_session)

    paths = [
        await TEI._check_run(db_session, run) for run in runs
    ]  # 저장 값과 대조 + 도구 T1~T5·T8
    built = [await EI.build_engine_input(db_session, run) for run in runs]

    assert paths == ["READY_FOR_REVIEW"] * 4
    assert calls == [(runs[0].company_id, 10)]
    assert all(b.store is built[0].store for b in built)
    assert engine_store_cache().stats().hits == 7

    other = TEI._by_code(await TEI._runs(db_session, "A_BEAUTY"), "AN-260115-001")
    await EI.build_engine_input(db_session, other)  # 다른 입력 순번 → 새로 읽음
    assert calls[-1] == (other.company_id, other.input_seq) and len(calls) == 2


async def test_concurrent_engine_runs_on_shared_store_do_not_change_it(
    db_session, engine_dir
):
    runs = await _shared_seq_runs(db_session)
    store = await EI.get_company_engine_store(db_session, runs[0].company_id, 10)
    inputs = [await EI._run_input(db_session, run) for run in runs]
    company_id = str(runs[0].company_id)
    before = _fingerprint(store)

    # 차례로 한 번
    expected = [EI._build_and_analyze(store, company_id, ri)[1]["sha"] for ri in inputs]
    assert _fingerprint(store) == before

    # 실행 4건 × 2번을 스레드 8개에서 동시에(asyncio.to_thread 와 같은 기본 스레드 풀 모양)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(EI._build_and_analyze, store, company_id, ri)
            for ri in inputs * 2
        ]
        results = [f.result()[1]["sha"] for f in futures]

    assert results == expected * 2
    assert expected == [run.decision_signature_sha256 for run in runs]
    assert _fingerprint(store) == before
