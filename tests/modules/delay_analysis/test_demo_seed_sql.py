from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.modules.data_sync.infrastructure.virtual_tms import VirtualTmsScenario
from scripts.seed_demo_runs import Engine, seed_demo

"""
    데모 시드(GLOW-89, scripts/seed_demo_runs.py) — 실제 DB·실제 엔진, 끝나면 롤백
      - 시나리오는 설계 문서(설계/GLOW-89_가상TMS_시나리오.json, 저장소 밖)와 같은 선적 3건을 여기 적는다.
      - 확인: 열린 지연 3건, 실행 3건 READY_FOR_REVIEW·설명 PASSED·완료 = 시작 + 1초, 결과 표 행,
        대응안 FEASIBLE·NEEDS_CHECK·NOT_FEASIBLE, 두 번 돌려도 새로 만들지 않음.
      - GLOWSHIP_ENGINE_DIR 이 없거나 확정 DB(A_BEAUTY, 마지막 SHIPMENT 수집 07-01)가 아니면 건너뛴다.
"""

COMPANY = "A_BEAUTY"
VIRTUAL_START = datetime(2026, 7, 2, tzinfo=UTC)


def _shipment(no, mode, wh, kr, stage, port, eta, sku):
    return {
        "shipmentNo": no,
        "mode": mode,
        "destWh": wh,
        "plannedKrRelease": kr,
        "stage": stage,
        "etaPort": port,
        "eta3pl": eta,
        "actual3pl": None,
        "items": [{"lineNo": 1, "sku": sku, "qty": 96}],
    }


SCENARIO = {
    "responses": [
        {
            "fetched_at": "2026-07-02T00:00:00Z",
            "_demo_company": COMPANY,
            "data": [
                _shipment("DEMO-0702-PRE", "AIR", "TMS-EWR", "2026-07-03", "PRE_DEPARTURE", None, "2026-09-01", "TMS-SKU-022"),
                _shipment("DEMO-0702-SEA", "OCEAN", "TMS-LAX", "2026-06-08", "AT_SEA", "2026-09-15", "2026-09-22", "TMS-SKU-019"),
                _shipment("DEMO-0702-PORT", "AIR", "TMS-EWR", "2026-06-24", "AT_PORT", "2026-06-29", "2026-07-25", "TMS-SKU-018"),
            ],
        }
    ]
}  # fmt: skip


async def _ready(db):
    if get_settings().glowship_engine_dir is None:
        pytest.skip("GLOWSHIP_ENGINE_DIR 이 없어 실제 엔진 시험을 건너뜀")
    last = await db.scalar(
        text(
            "SELECT max(r.started_at) FROM data_sync_runs r JOIN companies c USING (company_id)"
            " WHERE c.company_code = :c AND r.source_domain = 'SHIPMENT' AND r.applied_seq IS NOT NULL"
        ),
        {"c": COMPANY},
    )
    if last is None or last.date().isoformat() != "2026-07-01":
        pytest.skip("확정 데이터(마지막 SHIPMENT 수집 07-01)가 아닌 DB")


async def _seed(db):
    @asynccontextmanager
    async def same_session():
        yield db

    return await seed_demo(
        db,
        same_session,
        company_code=COMPANY,
        scenario=VirtualTmsScenario.from_json(SCENARIO),
        virtual_start=VIRTUAL_START,
        engine=Engine.load(),
    )


async def test_seed_creates_three_ready_runs_with_all_feasibility_kinds(
    db_write_session,
):
    db = db_write_session
    await _ready(db)

    seeded = await _seed(db)

    assert [(s.shipment_code, s.stage, s.status) for s in seeded] == [
        ("DEMO-0702-PORT", "AT_PORT", "READY_FOR_REVIEW"),
        ("DEMO-0702-PRE", "PRE_DEPARTURE", "READY_FOR_REVIEW"),
        ("DEMO-0702-SEA", "AT_SEA", "READY_FOR_REVIEW"),
    ]
    kinds = {feasibility for s in seeded for _, feasibility, _, _ in s.options}
    assert kinds == {"FEASIBLE", "NEEDS_CHECK", "NOT_FEASIBLE"}
    assert all(sum(r for *_, r in s.options) == 1 for s in seeded)  # 추천 1개씩

    runs = (
        await db.execute(
            text(
                """
                SELECT r.status, r.is_latest, r.trigger_type, r.started_at, r.finished_at,
                       r.explanation_validation_status, r.final_explanation_json IS NOT NULL AS has_explanation,
                       e.closed_at,
                       (SELECT count(*) FROM inventory_impact_results i WHERE i.analysis_run_id = r.analysis_run_id) AS impacts,
                       (SELECT count(*) FROM response_options o WHERE o.analysis_run_id = r.analysis_run_id) AS options,
                       (SELECT count(*) FROM analysis_run_data_versions v WHERE v.analysis_run_id = r.analysis_run_id) AS data_versions
                  FROM analysis_runs r
                  JOIN delay_events e USING (delay_event_id)
                  JOIN shipments s ON s.shipment_id = e.shipment_id
                 WHERE s.shipment_code LIKE 'DEMO-0702-%'
                """
            )
        )
    ).all()
    assert len(runs) == 3
    for run in runs:
        assert (run.status, run.is_latest, run.trigger_type) == (
            "READY_FOR_REVIEW",
            True,
            "AUTO",
        )
        assert run.closed_at is None  # 열린 지연 → 판단 API 18~20 대상
        assert run.finished_at == run.started_at + timedelta(seconds=1)
        assert run.started_at.date().isoformat() == "2026-07-02"  # 가상 시각(이슈 #50)
        assert (run.explanation_validation_status, run.has_explanation) == (
            "PASSED",
            True,
        )
        assert run.impacts > 0 and run.options > 0
        assert run.data_versions == 5  # 결과를 넣을 때 기록(도메인 5개)


async def test_seed_twice_does_not_create_again(db_write_session):
    db = db_write_session
    await _ready(db)
    first = await _seed(db)
    count = await db.scalar(text("SELECT count(*) FROM analysis_runs"))

    second = await _seed(db)

    assert len(first) == 3
    assert second == []
    assert await db.scalar(text("SELECT count(*) FROM analysis_runs")) == count
