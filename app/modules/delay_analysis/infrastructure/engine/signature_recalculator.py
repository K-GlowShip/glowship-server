import asyncio
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.delay_analysis.infrastructure.analysis_run import AnalysisRun
from app.modules.delay_analysis.infrastructure.engine.engine_input import (
    _calendar,
    _run_input,
    _RunInput,
    resolve_base_date,
)
from app.modules.delay_analysis.infrastructure.engine.engine_input_store import (
    engine_timestamp,
    load_company_engine_store,
)
from app.modules.delay_analysis.infrastructure.engine.engine_loader import (
    load_engine_module,
)
from app.modules.delay_analysis.infrastructure.engine.engine_operating_rules import (
    unsupported_operating_rules,
)
from app.modules.delay_analysis.infrastructure.engine.engine_store_cache import (
    get_company_engine_store,
)
from app.modules.delay_analysis.infrastructure.repository.stale_change_repository import (
    ScopedChange,
)

"""
    판정 서명 재계산기 (이슈 #75, STALE 재판정 2차 — D-13 ST-3 C안)
      - 엔진 stale.judge 와 같은 방식이다. 기준일 D·입력 확정 시각(confirm_t)·단계·예상 입고일은 실행을 만들 때 그대로 두고,
        이번 변경을 적용한 수집이 끝난 시각(knowledge_t)까지 알려진 입력으로 snapshot.build → pipeline.analyze 를 다시 돌려
        새 판정 서명 해시가 실행의 decision_signature_sha256 과 다르면 True.
          · D 는 build_engine_input 과 같이 실행의 입력 순번 저장소에서 resolve_base_date 로 정한다(실행에 저장돼 있지 않음).
          · 새 입력은 changes 의 마지막 적용 순번까지의 저장소, knowledge_t = 그 수집의 완료 시각.
      - 다르다고 보는 경우(선택 전 놓치지 않는 쪽): 실행에 서명이 없음, 수집 완료 시각이 없음,
        재계산 입력이 엔진 Hold(엔진 judge 와 같음)나 계산이 지원하지 않는 운영 기준이 됨.
      - 새 입력 순번 저장소는 캐시를 거치지 않는다. 수집 트랜잭션 안(커밋 전)에서 읽으므로, 되돌려지면 같은 순번이
        다른 내용으로 다시 쓰일 수 있다. 대신 이 재계산기 하나(트랜잭션 하나) 안에서는 순번별로 한 번만 읽는다.
        실행의 입력 순번 저장소는 이미 커밋된 순번이라 캐시(#60)를 쓴다.
      - 세션은 그 트랜잭션의 세션(워커 역할)이다. 읽기만 하고 커밋하지 않는다.
"""


class EngineSignatureRecalculator:
    def __init__(self, session: AsyncSession):
        self.session = session
        self._new_stores: dict[tuple, object] = {}

    async def signature_changed(
        self, run: AnalysisRun, changes: Sequence[ScopedChange]
    ) -> bool:
        if run.decision_signature_sha256 is None or not changes:
            return True
        last = max(changes, key=lambda c: c.applied_seq)
        if last.sync_finished_at is None:
            return True
        ri = await _run_input(self.session, run)
        base_store = await get_company_engine_store(
            self.session, run.company_id, run.input_seq
        )
        new_store = await self._new_store(run, last.applied_seq)
        sha = await asyncio.to_thread(
            _recalculated_sha,
            base_store,
            new_store,
            str(run.company_id),
            ri,
            engine_timestamp(last.sync_finished_at),
        )
        return sha != run.decision_signature_sha256

    async def _new_store(self, run: AnalysisRun, applied_seq: int):
        key = (run.company_id, applied_seq)
        if key not in self._new_stores:
            self._new_stores[key] = await load_company_engine_store(
                self.session, run.company_id, applied_seq
            )
        return self._new_stores[key]


def _recalculated_sha(
    base_store, new_store, company_id: str, ri: _RunInput, knowledge_t: str
) -> str | None:
    """같은 D·confirm_t 에서 knowledge_t 까지 알려진 입력으로 다시 계산한 서명 해시. 보류면 None."""
    snapshot = load_engine_module("snapshot")
    basis = resolve_base_date(base_store, company_id, ri.confirm_t)
    try:
        snap = snapshot.build(
            new_store,
            _calendar(),
            str(ri.shipment_id),
            basis.base_date,
            ri.confirm_t,
            stage=ri.stage,
            E=ri.expected.isoformat() if ri.expected is not None else None,
            knowledge_t=knowledge_t,
        )
    except snapshot.Hold:
        return None
    if unsupported_operating_rules(new_store, snap) is not None:
        return None
    return load_engine_module("pipeline").analyze(snap)["sha"]
