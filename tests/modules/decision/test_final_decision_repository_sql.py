import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.modules.decision.infrastructure.repository.final_decision_repository import (
    FinalDecisionRepository,
    NewFinalDecision,
)

"""
    최종 선택 저장소 SQL 테스트 (API 19, 실제 DB, 쓰기는 끝나면 롤백)
      - 확정 DB 의 모든 실행·대응안으로 재확인 입력을 읽어 독립 SQL 과 대조하고,
        롤백 트랜잭션에서 최종 선택을 추가해 DB 제약(uk2 실행당 1건, uk3 고객사 안 키 1건)을 확인합니다.
"""

UNKNOWN_ID = uuid.UUID("00000000-0000-7000-8000-000000000000")
DAY = date(2026, 7, 2)

_OPTIONS = text(
    """
    SELECT o.company_id, o.analysis_run_id, o.response_option_id, o.option_type,
           o.feasibility_status, r.status, r.is_latest, r.operating_rule_set_id,
           e.closed_at, s.current_expected_3pl_inbound_date, s.planned_kr_release_date,
           (SELECT count(*) FROM option_order_outcomes x
             WHERE x.response_option_id = o.response_option_id AND x.outcome = 'RECOVERED') AS recovered
      FROM response_options o
      JOIN analysis_runs r ON r.analysis_run_id = o.analysis_run_id
      JOIN delay_events e ON e.delay_event_id = r.delay_event_id
      JOIN shipments s ON s.shipment_id = e.shipment_id
     ORDER BY o.response_option_id
    """
)


async def _options(db):
    rows = (await db.execute(_OPTIONS)).all()
    if not rows:
        pytest.skip("대응안이 없는 DB")
    return rows


async def test_selection_target_matches_db_for_every_option(db_session):
    repo = FinalDecisionRepository(db_session)

    for row in await _options(db_session):
        target = await repo.find_selection_target(
            row.company_id, row.analysis_run_id, row.response_option_id, DAY
        )

        assert (target.status, target.is_latest, target.episode_open) == (
            row.status,
            row.is_latest,
            row.closed_at is None,
        )
        assert target.operating_rule_set_id == row.operating_rule_set_id
        assert (
            target.latest_expected_3pl_inbound_date,
            target.planned_kr_release_date,
            target.selection_day,
        ) == (row.current_expected_3pl_inbound_date, row.planned_kr_release_date, DAY)
        assert (target.option.option_type, target.option.feasibility_status) == (
            row.option_type,
            row.feasibility_status,
        )
        assert len(target.option.recovered_promised_dates) == row.recovered
        # 확정 데이터는 운영 기준 버전이 하나라 선택일의 적용 버전이 실행의 버전과 같다
        assert target.active_operating_rule_set_id == row.operating_rule_set_id
        assert all(isinstance(v, int) for v in vars(target.lead_times).values())


async def test_lead_times_follow_engine_rule(db_session):
    """도착 창고·공여 창고·한국 창고와 도착 권역으로 고른 값이 운영 기준 표의 값과 같다"""
    row = (await _options(db_session))[0]
    target = await FinalDecisionRepository(db_session).find_selection_target(
        row.company_id, row.analysis_run_id, row.response_option_id, DAY
    )
    expected = (
        await db_session.execute(
            text(
                """
                SELECT
                  (SELECT days FROM operating_lead_times l JOIN warehouses w USING (warehouse_id)
                    WHERE l.operating_rule_set_id = r.operating_rule_set_id
                      AND l.lead_time_type = 'OUTBOUND_HANDLING' AND w.warehouse_type = 'KR_ORIGIN') AS oh_kr,
                  (SELECT days FROM operating_lead_times l
                    WHERE l.operating_rule_set_id = r.operating_rule_set_id
                      AND l.lead_time_type = 'OUTBOUND_HANDLING' AND l.warehouse_id = s.warehouse_id) AS oh_dest,
                  (SELECT days FROM operating_lead_times l
                    WHERE l.operating_rule_set_id = r.operating_rule_set_id
                      AND l.lead_time_type = 'INBOUND_HANDLING' AND l.warehouse_id = s.warehouse_id) AS inbound_dest,
                  (SELECT days FROM operating_lead_times l
                    WHERE l.operating_rule_set_id = r.operating_rule_set_id
                      AND l.lead_time_type = 'CUSTOMER_DELIVERY' AND l.warehouse_id = s.warehouse_id
                      AND l.to_region_code = w.region_code) AS cd_same
                  FROM analysis_runs r
                  JOIN delay_events e ON e.delay_event_id = r.delay_event_id
                  JOIN shipments s ON s.shipment_id = e.shipment_id
                  JOIN warehouses w ON w.warehouse_id = s.warehouse_id
                 WHERE r.analysis_run_id = :r
                """
            ),
            {"r": row.analysis_run_id},
        )
    ).one()

    lt = target.lead_times
    assert (lt.oh_kr, lt.oh_dest, lt.inbound_dest, lt.cd_same) == tuple(expected)


async def test_option_of_other_run_is_none_and_unknown_run_is_none(db_session):
    repo = FinalDecisionRepository(db_session)
    rows = await _options(db_session)
    first = rows[0]
    other = next((r for r in rows if r.analysis_run_id != first.analysis_run_id), None)
    if other is None:
        pytest.skip("대응안이 있는 실행이 하나뿐인 DB")

    target = await repo.find_selection_target(
        first.company_id, first.analysis_run_id, other.response_option_id, DAY
    )

    assert target.option is None
    assert (
        await repo.find_selection_target(
            UNKNOWN_ID, first.analysis_run_id, first.response_option_id, DAY
        )
        is None
    )


async def test_active_rule_is_none_before_first_version(db_session):
    row = (await _options(db_session))[0]

    target = await FinalDecisionRepository(db_session).find_selection_target(
        row.company_id, row.analysis_run_id, row.response_option_id, date(2000, 1, 1)
    )

    assert target.active_operating_rule_set_id is None


async def test_find_by_idempotency_key(db_session):
    stored = (
        await db_session.execute(
            text(
                "SELECT company_id, final_decision_id, analysis_run_id, decided_at, checked_seq,"
                " response_option_id, reason_text, idempotency_key"
                " FROM final_decisions ORDER BY final_decision_id LIMIT 1"
            )
        )
    ).first()
    if stored is None:
        pytest.skip("최종 선택이 없는 DB")
    repo = FinalDecisionRepository(db_session)

    found = await repo.find_by_idempotency_key(
        stored.company_id, stored.idempotency_key
    )

    assert tuple(vars(found).values()) == tuple(stored)[1:7]
    assert (
        await repo.find_by_idempotency_key(UNKNOWN_ID, stored.idempotency_key) is None
    )
    assert await repo.find_by_idempotency_key(stored.company_id, "no-such-key") is None


async def _run_without_decision(db):
    row = (
        await db.execute(
            text(
                """
                SELECT o.company_id, o.analysis_run_id, o.response_option_id,
                       (SELECT u.user_id FROM users u WHERE u.company_id = o.company_id
                         AND u.role_code = 'CLIENT_LEAD' ORDER BY u.user_id LIMIT 1) AS lead_id
                  FROM response_options o
                 WHERE NOT EXISTS (SELECT 1 FROM final_decisions f
                                    WHERE f.analysis_run_id = o.analysis_run_id)
                 ORDER BY o.response_option_id LIMIT 1
                """
            )
        )
    ).first()
    if row is None or row.lead_id is None:
        pytest.skip("최종 선택이 없는 대응안·팀장이 없는 DB")
    return row


def _decision(row, **kwargs) -> NewFinalDecision:
    values = {
        "final_decision_id": uuid.uuid7(),
        "analysis_run_id": row.analysis_run_id,
        "user_id": row.lead_id,
        "response_option_id": row.response_option_id,
        "reason_text": "재고 영향이 가장 적음",
        "idempotency_key": f"test-{uuid.uuid7()}",
        "decided_at": datetime(2026, 7, 2, 0, 3, tzinfo=UTC),
        "checked_seq": 1,
    } | kwargs
    return NewFinalDecision(**values)


async def test_insert_final_decision_and_constraints(db_write_session):
    db = db_write_session
    row = await _run_without_decision(db)
    repo = FinalDecisionRepository(db)
    decision = _decision(row)

    await repo.insert_final_decision(row.company_id, decision)

    found = await repo.find_by_idempotency_key(row.company_id, decision.idempotency_key)
    assert (found.final_decision_id, found.analysis_run_id, found.checked_seq) == (
        decision.final_decision_id,
        row.analysis_run_id,
        1,
    )
    # 같은 실행의 두 번째 최종 선택은 DB 가 막는다 (uk2)
    with pytest.raises(IntegrityError, match="final_decisions_uk2"):
        await repo.insert_final_decision(row.company_id, _decision(row))


async def test_same_key_for_other_run_is_blocked(db_write_session):
    db = db_write_session
    rows = (
        await db.execute(
            text(
                """
                SELECT DISTINCT ON (o.analysis_run_id) o.company_id, o.analysis_run_id,
                       o.response_option_id,
                       (SELECT u.user_id FROM users u WHERE u.company_id = o.company_id
                         AND u.role_code = 'CLIENT_LEAD' ORDER BY u.user_id LIMIT 1) AS lead_id
                  FROM response_options o
                 WHERE NOT EXISTS (SELECT 1 FROM final_decisions f
                                    WHERE f.analysis_run_id = o.analysis_run_id)
                 ORDER BY o.analysis_run_id, o.response_option_id
                """
            )
        )
    ).all()
    same_company = (
        [r for r in rows if r.company_id == rows[0].company_id] if rows else []
    )
    if len(same_company) < 2:
        pytest.skip("최종 선택이 없는 실행이 한 고객사에 둘 이상 없는 DB")
    first, second = same_company[:2]
    repo = FinalDecisionRepository(db)
    await repo.insert_final_decision(
        first.company_id, _decision(first, idempotency_key="same-key")
    )

    with pytest.raises(IntegrityError, match="final_decisions_uk3"):
        await repo.insert_final_decision(
            second.company_id, _decision(second, idempotency_key="same-key")
        )
