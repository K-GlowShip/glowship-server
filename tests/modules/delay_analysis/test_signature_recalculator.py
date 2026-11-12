import uuid
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.modules.delay_analysis.infrastructure.analysis_run import AnalysisRun
from app.modules.delay_analysis.infrastructure.engine import signature_recalculator
from app.modules.delay_analysis.infrastructure.engine.engine_input import _RunInput
from app.modules.delay_analysis.infrastructure.engine.signature_recalculator import (
    EngineSignatureRecalculator,
)
from app.modules.delay_analysis.infrastructure.repository.stale_change_repository import (
    ScopedChange,
)

"""
    판정 서명 재계산기 (이슈 #75) — DB·엔진 없이 분기만 본다. 실제 엔진 계산은 test_signature_recalculator_sql.py
      - 서명이 없거나, 변경이 없거나, 수집 완료 시각이 없으면 계산하지 않고 다름(놓치지 않는 쪽)
      - 다시 계산한 해시가 실행의 해시와 같으면 False, 다르거나 보류(None)면 True
      - 새 입력 순번 저장소는 재계산기 하나 안에서 마지막 순번 하나만 들고(같은 순번이면 다시 쓰고), 실행의 입력 순번 저장소는 캐시 함수로 읽는다
"""

COMPANY_ID = uuid.uuid7()
SHA = "a" * 64
RUN_INPUT = _RunInput(
    shipment_id=uuid.uuid7(),
    confirm_t="2026-07-03T00:03:00Z",
    run_date=date(2026, 7, 3),
    planned=date(2026, 7, 1),
    stage="AT_PORT",
    expected=date(2026, 7, 6),
)


def _run(sha: str | None = SHA) -> AnalysisRun:
    return AnalysisRun(
        analysis_run_id=uuid.uuid7(),
        company_id=COMPANY_ID,
        delay_event_id=uuid.uuid7(),
        input_seq=921,
        status="READY_FOR_REVIEW",
        is_latest=True,
        decision_signature_sha256=sha,
        started_at=datetime(2026, 7, 3, 0, 3, tzinfo=UTC),
    )


def _change(seq: int, finished: datetime | None = None) -> ScopedChange:
    return ScopedChange(
        applied_seq=seq,
        sync_started_at=datetime(2026, 7, 3, 0, 12, tzinfo=UTC),
        sync_finished_at=finished,
        entity_type="ORDER",
        change_type="INSERT",
        entity_ref=uuid.uuid7(),
        sku_id=uuid.uuid7(),
        warehouse_id=uuid.uuid7(),
        changed_fields=(),
        change_summary="새 주문",
    )


FINISHED = datetime(2026, 7, 3, 0, 12, 30, tzinfo=UTC)


@pytest.fixture
def fake_engine(monkeypatch):
    """DB·엔진 대신 기록만 하는 가짜. recalculated 에 다시 계산한 해시를 넣는다(None = 보류)."""
    calls = {"base": [], "new": [], "sha": []}
    state = {"recalculated": SHA}

    async def run_input(session, run):
        return RUN_INPUT

    async def cached(session, company_id, input_seq):
        calls["base"].append(input_seq)
        return f"base-{input_seq}"

    async def load(session, company_id, input_seq):
        calls["new"].append(input_seq)
        return f"new-{input_seq}"

    def recalc(base_store, new_store, company_id, ri, knowledge_t):
        calls["sha"].append((base_store, new_store, company_id, knowledge_t))
        return state["recalculated"]

    monkeypatch.setattr(signature_recalculator, "_run_input", run_input)
    monkeypatch.setattr(signature_recalculator, "get_company_engine_store", cached)
    monkeypatch.setattr(signature_recalculator, "load_company_engine_store", load)
    monkeypatch.setattr(signature_recalculator, "_recalculated_sha", recalc)
    return calls, state


@pytest.mark.parametrize(
    ("run", "changes"),
    [
        (_run(sha=None), [_change(922, FINISHED)]),  # 서명 없음
        (_run(), []),  # 변경 없음
        (_run(), [_change(922, None)]),  # 수집 완료 시각 없음
    ],
)
async def test_changed_without_calculation_when_input_is_missing(
    fake_engine, run, changes
):
    calls, _ = fake_engine

    assert await EngineSignatureRecalculator(None).signature_changed(run, changes)
    assert calls == {"base": [], "new": [], "sha": []}


@pytest.mark.parametrize(
    ("recalculated", "changed"),
    [(SHA, False), ("b" * 64, True), (None, True)],  # 같음 / 다름 / 보류
)
async def test_compares_recalculated_hash_with_run_hash(
    fake_engine, recalculated, changed
):
    calls, state = fake_engine
    state["recalculated"] = recalculated

    result = await EngineSignatureRecalculator(None).signature_changed(
        _run(), [_change(922, FINISHED), _change(924, FINISHED)]
    )

    assert result is changed
    # D 는 실행의 입력 순번 저장소, 새 입력은 마지막 순번 저장소, 알려진 시각은 그 수집의 완료 시각(초 단위)
    assert calls["sha"] == [
        ("base-921", "new-924", str(COMPANY_ID), "2026-07-03T00:12:30Z")
    ]


async def test_new_store_is_read_once_per_seq_within_one_recalculator(fake_engine):
    calls, _ = fake_engine
    recalculator = EngineSignatureRecalculator(None)

    for _ in range(2):  # 같은 수집에 걸린 실행 두 개
        await recalculator.signature_changed(_run(), [_change(924, FINISHED)])
    await recalculator.signature_changed(_run(), [_change(925, FINISHED)])

    assert calls["new"] == [924, 925]
    assert calls["base"] == [921, 921, 921]  # 실행 입력 순번은 캐시 함수가 맡는다


async def test_only_the_last_new_store_is_kept(fake_engine):
    """새 순번 저장소는 마지막 하나만 든다(메모리, PR #116 리뷰). 지난 순번으로 돌아가면 다시 읽는다"""
    calls, _ = fake_engine
    recalculator = EngineSignatureRecalculator(None)

    for seq in (924, 925, 924):
        await recalculator.signature_changed(_run(), [_change(seq, FINISHED)])

    assert calls["new"] == [924, 925, 924]


async def test_engine_error_counts_as_changed(fake_engine, monkeypatch, caplog):
    """엔진 쪽 오류는 수집·결과 저장을 멈추지 않고 다름(놓치지 않는 쪽)으로 본다"""

    def broken(*args):
        raise RuntimeError("엔진 계산 오류")

    monkeypatch.setattr(signature_recalculator, "_recalculated_sha", broken)

    assert await EngineSignatureRecalculator(None).signature_changed(
        _run(), [_change(924, FINISHED)]
    )
    assert "판정 서명 재계산 실패" in caplog.text


async def test_db_error_is_raised(fake_engine, monkeypatch):
    """DB 오류는 트랜잭션이 깨졌으므로 그대로 올린다"""

    async def broken(session, company_id, input_seq):
        raise SQLAlchemyError("DB 오류")

    monkeypatch.setattr(signature_recalculator, "load_company_engine_store", broken)

    with pytest.raises(SQLAlchemyError):
        await EngineSignatureRecalculator(None).signature_changed(
            _run(), [_change(924, FINISHED)]
        )


@pytest.mark.parametrize(("engine_dir", "expected"), [(None, False), ("C:/e", True)])
def test_recalculator_only_when_engine_is_configured(monkeypatch, engine_dir, expected):
    monkeypatch.setattr(
        signature_recalculator,
        "get_settings",
        lambda: SimpleNamespace(glowship_engine_dir=engine_dir),
    )

    made = signature_recalculator.recalculator_or_none(None)

    assert isinstance(made, EngineSignatureRecalculator) is expected
