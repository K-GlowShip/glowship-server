import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.modules.data_sync.infrastructure.data_sync_enums import SourceDomain
from app.modules.data_sync.infrastructure.virtual_tms import VirtualTmsScenario
from scripts.seed_demo_runs import (
    Engine,
    load_supplement,
    seed_demo,
    supplement_records,
)
from tests.modules.delay_analysis.test_demo_seed_sql import SCENARIO

"""
    데모 시드 보완 하루 (GLOW-89 후속, 위험 주문) — 실제 DB, 끝나면 롤백
      - 보완 파일(census tools/synth2026/extend_day.py 결과)의 07-01 주문·재고를 관측일 07-02 가상 시각에 다섯 원천 수집으로 넣는다.
      - 작은 보완(주문 1건·재고 1행)으로 수집 모양을 보고, 실제 보완 파일(DEMO_SUPPLEMENT_PATH)이 있으면 끝까지 돌려
        결과(해상 선적 READY_ORDER_RISK·지연 때문인 위험 주문 3건, 항구 FORECAST_ONLY_READY, 출항 전 NO_RISK)를 본다.
      - 확정 DB(마지막 재고 06-30)가 아니면 건너뛴다.
"""

COMPANY = "A_BEAUTY"
SMALL = {
    "company_code": COMPANY,
    "day": "2026-07-01",
    "observed_at": "2026-07-02",
    "order_response": [
        {
            "order_code": "A-ORD-20260701-9999",
            "sales_channel": "SEPHORA",
            "customer_region": "WEST",
            "warehouse_code": "WH-WEST",
            "ordered_at": "2026-07-01T10:00:00Z",
            "promised_delivery_at": "2026-07-08T23:59:59Z",
            "status": "OPEN",
            "lines": [
                {
                    "line_no": 1,
                    "sku_code": "SKU-001",
                    "ordered_quantity": 2,
                    "shipped_quantity": 0,
                    "line_status": "ACTIVE",
                }
            ],
        }
    ],
    "inventory_response": [
        {
            "inventory_date": "2026-07-01",
            "warehouse_code": "WMS-LAX01",
            "sku_code": "WMS-SKU-001",
            "on_hand_quantity": 10,
            "reserved_quantity": 2,
            "available_quantity": 8,
            "outbound_quantity": 0,
        }
    ],
}


async def _confirmed_company(db):
    company_id = await db.scalar(
        text("SELECT company_id FROM companies WHERE company_code = :c"),
        {"c": COMPANY},
    )
    last = await db.scalar(
        text("SELECT max(inventory_date) FROM inventory_daily WHERE company_id = :c"),
        {"c": company_id},
    )
    if company_id is None or last != date(2026, 6, 30):
        pytest.skip("확정 데이터(마지막 재고 06-30)가 아닌 DB")
    return company_id


def test_supplement_records_by_source():
    records = supplement_records(SMALL)

    assert {d: len(r) for d, r in records.items()} == {
        SourceDomain.PRODUCT: 0,
        SourceDomain.WAREHOUSE: 0,
        SourceDomain.ORDER: 1,
        SourceDomain.SHIPMENT: 0,
        SourceDomain.INVENTORY: 1,
    }
    order = records[SourceDomain.ORDER][0]
    assert order.ordered_at == datetime(2026, 7, 1, 10, tzinfo=UTC)
    assert order.lines[0].sku_code == "SKU-001"
    assert records[SourceDomain.INVENTORY][0].inventory_date == date(2026, 7, 1)


async def test_supplement_loads_five_syncs_at_observed_day_slots(db_write_session):
    db = db_write_session
    company_id = await _confirmed_company(db)

    loaded = await load_supplement(db, company_id, SMALL)
    again = await load_supplement(db, company_id, SMALL)

    runs = (
        await db.execute(
            text(
                "SELECT source_domain, started_at, status, loaded_record_count FROM data_sync_runs"
                " WHERE company_id = :c AND started_at >= '2026-07-02' ORDER BY started_at"
            ),
            {"c": company_id},
        )
    ).all()
    assert (loaded, again) == (True, False)  # 그날 재고가 있으면 다시 넣지 않음
    assert [
        (r.source_domain, r.started_at.strftime("%H:%M"), r.status) for r in runs
    ] == [
        ("PRODUCT", "00:00", "SUCCEEDED"),
        ("WAREHOUSE", "00:01", "SUCCEEDED"),
        ("ORDER", "00:02", "SUCCEEDED"),
        ("SHIPMENT", "00:03", "SUCCEEDED"),
        ("INVENTORY", "00:04", "SUCCEEDED"),
    ]
    assert [r.loaded_record_count for r in runs] == [0, 0, 1, 0, 1]
    assert (
        await db.scalar(
            text("SELECT status FROM orders WHERE order_code = 'A-ORD-20260701-9999'")
        )
        == "OPEN"
    )


@pytest.mark.skipif(
    not os.environ.get("DEMO_SUPPLEMENT_PATH"),
    reason="DEMO_SUPPLEMENT_PATH(보완 파일)가 없어 끝까지 돌리는 시험을 건너뜀",
)
async def test_seed_with_supplement_gives_order_risk(db_write_session):
    db = db_write_session
    await _confirmed_company(db)
    if get_settings().glowship_engine_dir is None:
        pytest.skip("GLOWSHIP_ENGINE_DIR 이 없어 실제 엔진 시험을 건너뜀")
    supplement = json.loads(
        Path(os.environ["DEMO_SUPPLEMENT_PATH"]).read_text(encoding="utf-8")
    )
    scenario = json.loads(json.dumps(SCENARIO))
    for response in scenario["responses"]:
        response["fetched_at"] = "2026-07-03T00:00:00Z"  # 보완 하루 다음 날

    @asynccontextmanager
    async def same_session():
        yield db

    seeded = await seed_demo(
        db,
        same_session,
        company_code=COMPANY,
        scenario=VirtualTmsScenario.from_json(scenario),
        virtual_start=datetime(2026, 7, 2, tzinfo=UTC),
        engine=Engine.load(),
        supplement=supplement,
    )

    assert [(s.shipment_code, s.status, s.result_class) for s in seeded] == [
        ("DEMO-0702-PORT", "READY_FOR_REVIEW", "FORECAST_ONLY_READY"),
        ("DEMO-0702-PRE", "NO_RISK", "PURE_NO_RISK"),
        ("DEMO-0702-SEA", "READY_FOR_REVIEW", "READY_ORDER_RISK"),
    ]
    risks = (
        await db.execute(
            text(
                """
                SELECT a.risk_attribution, a.promised_delivery_at::date AS promised,
                       a.baseline_expected_delivery_at::date AS baseline, a.expected_delivery_at::date AS expected
                  FROM at_risk_orders a JOIN analysis_runs r USING (analysis_run_id)
                  JOIN delay_events e USING (delay_event_id) JOIN shipments s ON s.shipment_id = e.shipment_id
                 WHERE s.shipment_code = 'DEMO-0702-SEA'
                """
            )
        )
    ).all()
    assert len(risks) == 3
    assert all(
        (r.risk_attribution, r.promised) == ("DELAY_INDUCED", date(2026, 7, 8))
        and r.baseline <= r.promised < r.expected
        for r in risks
    )
    run_days = await db.scalar(
        text(
            "SELECT array_agg(DISTINCT r.started_at::date) FROM analysis_runs r"
            " JOIN delay_events e USING (delay_event_id) JOIN shipments s ON s.shipment_id = e.shipment_id"
            " WHERE s.shipment_code LIKE 'DEMO-0702-%'"
        )
    )
    assert run_days == [date(2026, 7, 3)]
