from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.modules.delay_analysis.application.run_creation.auto_run_service import (
    previous_run_stale_reason,
)
from app.modules.delay_analysis.infrastructure.analysis_enums import (
    StaleReasonCode,
    TriggerReason,
)
from tests.modules.data_sync.test_ingest_episode_sql import (
    DAY1,
    DAY2,
    _delayed_new_shipment,
    _ingest,
)
from tests.modules.data_sync.test_ingest_service_sql import _company

"""
    지연 감지 → AUTO 분석 실행 생성 (GLOW-18, 이슈 #34) — 실제 DB, 끝나면 롤백
      - SHIPMENT 수집이 판정한 결과마다 실행을 만든다: 시작 v1, 후속·회복 → 이전 실행 STALE 뒤 새 버전, 입고 → 없음.
      - 실행 시각 = 그 수집의 확정 시작(이슈 #50), 원인 스냅샷 = 판정한 스냅샷, 상태 DETECTED(분석 시작은 GLOW-128).
"""

DAY3 = DAY2 + timedelta(days=1)


async def _runs(db, company_id, shipment_code):
    return (
        await db.execute(
            text(
                """
                SELECT r.analysis_run_id, r.version_no, r.parent_analysis_run_id, r.trigger_type,
                       r.trigger_reason, r.trigger_shipment_schedule_snapshot_id, r.status, r.is_latest,
                       r.started_at, r.input_seq, r.stale_primary_reason, r.stale_detected_seq,
                       e.closed_at, e.close_reason,
                       (SELECT count(*) FROM analysis_run_data_versions v
                         WHERE v.analysis_run_id = r.analysis_run_id) AS data_versions
                  FROM analysis_runs r
                  JOIN delay_events e USING (delay_event_id)
                  JOIN shipments s ON s.shipment_id = e.shipment_id
                 WHERE r.company_id = :c AND s.shipment_code = :s
                 ORDER BY r.version_no
                """
            ),
            {"c": company_id, "s": shipment_code},
        )
    ).all()


async def test_새_지연이면_v1_AUTO_실행을_만든다(db_write_session):
    db = db_write_session
    company_id = await _company(db)
    shipment = await _delayed_new_shipment(db, company_id)

    result = await _ingest(db, DAY1, company_id, [shipment])

    [run] = await _runs(db, company_id, shipment.shipment_code)
    assert result.created_run_ids == (run.analysis_run_id,)
    assert (run.version_no, run.trigger_type, run.trigger_reason, run.status) == (
        1,
        "AUTO",
        "EPISODE_STARTED",
        "DETECTED",
    )
    assert run.is_latest and run.parent_analysis_run_id is None
    assert (
        run.trigger_shipment_schedule_snapshot_id
        == result.episode_outcomes[0].trigger_snapshot_id
    )
    # 실행 시각 = 이 수집의 확정 시작, 입력 순번 = 이 수집의 적용 순번
    assert (run.started_at, run.input_seq) == (DAY1, result.applied_seq)
    assert run.data_versions > 0


@pytest.mark.parametrize(
    ("change", "reason", "stale_reason"),
    [
        ({"days": 40}, "SCHEDULE_WORSENED", "DELAY_SCHEDULE_CHANGED"),
        ({"stage": "AT_PORT"}, "STAGE_CHANGED", "DELAY_STAGE_CHANGED"),
    ],
)
async def test_후속_판정은_이전_실행을_STALE_로_바꾸고_새_버전을_만든다(
    db_write_session, change, reason, stale_reason
):
    db = db_write_session
    company_id = await _company(db)
    shipment = await _delayed_new_shipment(db, company_id)
    await _ingest(db, DAY1, company_id, [shipment])
    later = replace(
        shipment,
        current_stage=change.get("stage", shipment.current_stage),
        current_expected_3pl_inbound_date=shipment.current_expected_3pl_inbound_date
        + timedelta(days=change.get("days", 0)),
    )

    result = await _ingest(db, DAY2, company_id, [later])

    first, second = await _runs(db, company_id, shipment.shipment_code)
    assert (first.status, first.is_latest, first.stale_primary_reason) == (
        "STALE",
        False,
        stale_reason,
    )
    assert first.stale_detected_seq == result.applied_seq
    assert (second.version_no, second.trigger_reason, second.status) == (
        2,
        reason,
        "DETECTED",
    )
    assert second.parent_analysis_run_id == first.analysis_run_id
    assert second.is_latest and second.started_at == DAY2
    assert result.created_run_ids == (second.analysis_run_id,)


async def test_회복이면_DELAY_RECOVERED_실행을_만든_뒤_에피소드를_닫는다(
    db_write_session,
):
    """이전 실행 STALE 은 에피소드를 닫기 전이라 동결로 건너뛰지 않는다(stale_service._unfrozen)"""
    db = db_write_session
    company_id = await _company(db)
    shipment = await _delayed_new_shipment(db, company_id)
    await _ingest(db, DAY1, company_id, [shipment])
    planned = shipment.current_expected_3pl_inbound_date - timedelta(days=30)

    await _ingest(
        db,
        DAY2,
        company_id,
        [replace(shipment, current_expected_3pl_inbound_date=planned)],
    )

    first, second = await _runs(db, company_id, shipment.shipment_code)
    assert (first.status, first.stale_primary_reason) == (
        "STALE",
        "DELAY_SCHEDULE_CHANGED",
    )
    assert (second.trigger_reason, second.status, second.is_latest) == (
        "DELAY_RECOVERED",
        "DETECTED",
        True,
    )
    assert (second.closed_at, second.close_reason) == (DAY2, "RECOVERED")


async def test_입고로_닫히면_실행을_만들지_않고_이전_실행도_그대로(db_write_session):
    db = db_write_session
    company_id = await _company(db)
    shipment = await _delayed_new_shipment(db, company_id)
    await _ingest(db, DAY1, company_id, [shipment])

    result = await _ingest(
        db,
        DAY2,
        company_id,
        [
            replace(
                shipment,
                current_stage="RECEIVED",
                actual_3pl_inbound_date=shipment.current_expected_3pl_inbound_date,
            )
        ],
    )

    [run] = await _runs(db, company_id, shipment.shipment_code)
    assert result.created_run_ids == ()
    assert (run.status, run.is_latest, run.close_reason) == (
        "DETECTED",
        True,
        "RECEIVED",
    )


async def test_같은_수집을_다시_보내도_실행은_한_번(db_write_session):
    """같은 값 재수집은 스냅샷을 남기지 않아 판정이 없고 실행도 늘지 않는다"""
    db = db_write_session
    company_id = await _company(db)
    shipment = await _delayed_new_shipment(db, company_id)
    await _ingest(db, DAY1, company_id, [shipment])

    again = await _ingest(db, DAY3, company_id, [shipment])

    assert again.created_run_ids == ()
    assert len(await _runs(db, company_id, shipment.shipment_code)) == 1


@pytest.mark.parametrize(
    ("trigger", "expected"),
    [
        (TriggerReason.STAGE_CHANGED, StaleReasonCode.DELAY_STAGE_CHANGED),
        (TriggerReason.SCHEDULE_WORSENED, StaleReasonCode.DELAY_SCHEDULE_CHANGED),
        (TriggerReason.SCHEDULE_IMPROVED, StaleReasonCode.DELAY_SCHEDULE_CHANGED),
        (TriggerReason.DELAY_RECOVERED, StaleReasonCode.DELAY_SCHEDULE_CHANGED),
    ],
)
def test_이전_실행_STALE_사유는_엔진_하네스와_같다(trigger, expected):
    assert previous_run_stale_reason(trigger) == expected


async def _drop_runs(db, company_id, shipment_code):
    """시작 때 실행을 만들지 못한 열린 지연을 만든다(이 PR 전 코드로 열렸거나 실행 생성이 건너뛰어진 경우)"""
    await db.execute(
        text(
            """
            DELETE FROM analysis_run_data_versions v USING analysis_runs r, delay_events e, shipments s
             WHERE v.analysis_run_id = r.analysis_run_id AND r.delay_event_id = e.delay_event_id
               AND e.shipment_id = s.shipment_id AND s.company_id = :c AND s.shipment_code = :s
            """
        ),
        {"c": company_id, "s": shipment_code},
    )
    await db.execute(
        text(
            """
            DELETE FROM analysis_runs r USING delay_events e, shipments s
             WHERE r.delay_event_id = e.delay_event_id AND e.shipment_id = s.shipment_id
               AND s.company_id = :c AND s.shipment_code = :s
            """
        ),
        {"c": company_id, "s": shipment_code},
    )


async def test_실행이_없는_열린_지연의_후속_판정은_첫_실행을_지연_시작으로_만든다(
    db_write_session,
):
    """#63 리뷰 [권장] 재현 1: 수정 전에는 EPISODE_START_MISMATCH(ck18)로 수집 전체가 FAILED"""
    db = db_write_session
    company_id = await _company(db)
    shipment = await _delayed_new_shipment(db, company_id)
    await _ingest(db, DAY1, company_id, [shipment])
    await _drop_runs(db, company_id, shipment.shipment_code)
    worse = replace(
        shipment,
        current_expected_3pl_inbound_date=shipment.current_expected_3pl_inbound_date
        + timedelta(days=10),
    )

    result = await _ingest(db, DAY2, company_id, [worse])

    [run] = await _runs(db, company_id, shipment.shipment_code)
    assert result.episode_outcomes[0].trigger_reason == TriggerReason.SCHEDULE_WORSENED
    assert (run.version_no, run.trigger_reason, run.status, run.is_latest) == (
        1,
        "EPISODE_STARTED",
        "DETECTED",
        True,
    )
    assert run.trigger_shipment_schedule_snapshot_id == (
        result.episode_outcomes[0].trigger_snapshot_id
    )
    assert result.created_run_ids == (run.analysis_run_id,)


async def test_실행이_없는_지연이_회복으로_닫히면_실행을_만들지_않는다(
    db_write_session,
):
    db = db_write_session
    company_id = await _company(db)
    shipment = await _delayed_new_shipment(db, company_id)
    await _ingest(db, DAY1, company_id, [shipment])
    await _drop_runs(db, company_id, shipment.shipment_code)
    planned = shipment.current_expected_3pl_inbound_date - timedelta(days=30)

    result = await _ingest(
        db,
        DAY2,
        company_id,
        [replace(shipment, current_expected_3pl_inbound_date=planned)],
    )

    assert result.created_run_ids == ()
    assert await _runs(db, company_id, shipment.shipment_code) == []
    assert (
        await db.scalar(
            text(
                "SELECT e.close_reason FROM delay_events e JOIN shipments s USING (shipment_id)"
                " WHERE s.shipment_code = :s"
            ),
            {"s": shipment.shipment_code},
        )
        == "RECOVERED"
    )


async def _hide_operating_rules(db, company_id):
    """그날 적용되는 운영 기준이 없는 고객사(새 고객사)를 만든다: 기준의 적용 시작일을 먼 뒤로 민다(롤백됨)"""
    await db.execute(
        text(
            "UPDATE operating_rule_sets SET effective_from = DATE '2099-01-01' WHERE company_id = :c"
        ),
        {"c": company_id},
    )


async def test_운영_기준이_없으면_실행만_건너뛰고_수집은_적재한다(db_write_session):
    """#63 리뷰 [권장] 재현 2: 수정 전에는 OPERATING_RULE_MISSING 으로 수집 FAILED·새 선적 0건"""
    db = db_write_session
    company_id = await _company(db)
    shipment = await _delayed_new_shipment(db, company_id)
    await _hide_operating_rules(db, company_id)

    result = await _ingest(db, DAY1, company_id, [shipment])

    loaded = await db.scalar(
        text(
            "SELECT count(*) FROM shipments WHERE company_id = :c AND shipment_code = :s"
        ),
        {"c": company_id, "s": shipment.shipment_code},
    )
    assert loaded == 1  # 선적은 적재됨
    assert [o.action for o in result.episode_outcomes] == ["START"]  # 에피소드는 열림
    assert result.created_run_ids == ()
    assert [(s.delay_event_id, s.code) for s in result.skipped_auto_runs] == [
        (result.episode_outcomes[0].delay_event_id, "OPERATING_RULE_MISSING")
    ]
    assert await _runs(db, company_id, shipment.shipment_code) == []


async def test_건너뛴_판정은_이전_실행_STALE_도_되돌리고_다음_후속에서_새_버전을_만든다(
    db_write_session,
):
    db = db_write_session
    company_id = await _company(db)
    shipment = await _delayed_new_shipment(db, company_id)
    await _ingest(db, DAY1, company_id, [shipment])
    restore = (
        await db.execute(
            text(
                "SELECT operating_rule_set_id, effective_from FROM operating_rule_sets WHERE company_id = :c"
            ),
            {"c": company_id},
        )
    ).all()
    await _hide_operating_rules(db, company_id)
    worse = replace(
        shipment,
        current_expected_3pl_inbound_date=shipment.current_expected_3pl_inbound_date
        + timedelta(days=10),
    )

    skipped = await _ingest(db, DAY2, company_id, [worse])

    [first] = await _runs(db, company_id, shipment.shipment_code)
    assert [s.code for s in skipped.skipped_auto_runs] == ["OPERATING_RULE_MISSING"]
    assert (first.status, first.is_latest) == ("DETECTED", True)  # STALE 도 함께 되돌림

    for row in restore:  # 운영 기준을 다시 넣은 뒤 다음 후속 판정
        await db.execute(
            text(
                "UPDATE operating_rule_sets SET effective_from = :f WHERE operating_rule_set_id = :i"
            ),
            {"f": row.effective_from, "i": row.operating_rule_set_id},
        )
    later = replace(worse, current_stage="AT_PORT")
    result = await _ingest(db, DAY3, company_id, [later])

    first, second = await _runs(db, company_id, shipment.shipment_code)
    assert (first.status, first.stale_primary_reason) == (
        "STALE",
        "DELAY_STAGE_CHANGED",
    )
    assert (second.version_no, second.trigger_reason, second.is_latest) == (
        2,
        "STAGE_CHANGED",
        True,
    )
    assert result.skipped_auto_runs == ()
