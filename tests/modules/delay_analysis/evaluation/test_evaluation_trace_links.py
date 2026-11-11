import json

import pytest
from real_path_support import (
    answer_snapshot,
    differences,
    fix_explanation_canary,
    pick_runs,
    require_engine,
    rerun,
    state_problems,
)
from sqlalchemy import text

from app.modules.delay_analysis.application.agent.tracing import RunTracer
from tests.modules.delay_analysis.test_agent_tracing import MemorySink

"""
    실제 실행의 추적과 감사 연결 (GLOW-58 2단계 D 갈래)
      - D1·D2 추적: 위험 주문이 있는 READY 실행 1건을 실제 경로로 돌리며 실행기의 tracer_factory 로 추적기를 단다.
        실행 시작 → 도구 시도 14건 → 설명 시도 1건 → 실행 끝이 실행 ID 하나로 이어지고, 그 실행의 실제 주문 코드·
        선적 코드가 기록에 원문으로 남지 않는다(agentflow 8.1, docs/agent-flow/agent-tracing.md 3절).
        앱 실행기(app_runner)는 아직 tracer_factory 를 넘기지 않는다. 여기서는 실행기에 직접 넘긴다.
      - D3 감사 연결(NFR-007 검증 방법): 최종 선택까지 간 실행에서 실행 ID 하나로 감지 이벤트(지연 에피소드)·
        시작 스냅샷·입력 데이터 버전 5건·결과 행·설명·팀원 초안·최종 선택이 이어진다. 읽기만 한다.
"""

READY_WITH_RISKS = {("A_BEAUTY", "AN-260115-001"): "READY_FOR_REVIEW"}

_CODES = text(
    """
    SELECT o.order_code AS code
      FROM at_risk_orders a
      JOIN order_lines ol ON ol.order_line_id = a.order_line_id
      JOIN orders o ON o.order_id = ol.order_id
     WHERE a.analysis_run_id = :id
    UNION
    SELECT s.shipment_code
      FROM analysis_runs r
      JOIN delay_events e USING (delay_event_id)
      JOIN shipments s ON s.shipment_id = e.shipment_id
     WHERE r.analysis_run_id = :id
    """
)
_FINALIZED = text(
    """
    SELECT r.analysis_run_id, r.run_code, r.trigger_type, r.input_seq, r.decision_signature_sha256,
           r.final_explanation_json, r.trigger_shipment_schedule_snapshot_id, r.parent_analysis_run_id,
           e.shipment_id, e.delay_event_id
      FROM analysis_runs r JOIN delay_events e USING (delay_event_id)
     WHERE r.status = 'FINALIZED'
     ORDER BY r.run_code
    """
)
_LINKS = text(
    """
    SELECT (SELECT count(*) FROM analysis_run_data_versions v JOIN data_sync_runs d USING (data_sync_run_id)
             WHERE v.analysis_run_id = :id AND d.applied_seq <= :seq) AS versions,
           (SELECT count(DISTINCT d.source_domain) FROM analysis_run_data_versions v
              JOIN data_sync_runs d USING (data_sync_run_id) WHERE v.analysis_run_id = :id) AS domains,
           (SELECT count(*) FROM inventory_impact_results WHERE analysis_run_id = :id) AS impacts,
           (SELECT count(*) FROM response_options WHERE analysis_run_id = :id) AS options,
           (SELECT count(*) FROM member_drafts WHERE analysis_run_id = :id) AS drafts,
           (SELECT count(*) FROM final_decisions f JOIN response_options o
                ON o.response_option_id = f.response_option_id AND o.analysis_run_id = f.analysis_run_id
             WHERE f.analysis_run_id = :id) AS decisions,
           (SELECT s.shipment_id FROM shipment_schedule_snapshots s
             WHERE s.shipment_schedule_snapshot_id = :snapshot) AS snapshot_shipment,
           (SELECT p.delay_event_id FROM analysis_runs p WHERE p.analysis_run_id = :parent) AS parent_event
    """
)


async def test_traced_real_run_links_events_by_run_id_and_masks_codes(
    db_write_session, monkeypatch
):
    """D1·D2: 실제 묶음 + 추적기. 사건이 실행 ID 하나로 이어지고 실제 주문·선적 코드가 기록에 없다."""
    require_engine()
    db = db_write_session
    (run,) = await pick_runs(db, READY_WITH_RISKS)
    fix_explanation_canary(monkeypatch)
    answer = await answer_snapshot(db, run)
    codes = (await db.scalars(_CODES, {"id": run.analysis_run_id})).all()
    sink = MemorySink()

    result = await rerun(
        db,
        run,
        tracer_factory=lambda started: RunTracer(
            started.analysis_run_id, started.company_id, [sink]
        ),
    )
    events = sink.events
    kinds = [event.kind for event in events]
    tools = [event for event in events if event.kind == "TOOL_ATTEMPT"]
    explanations = [event for event in events if event.kind == "EXPLANATION_ATTEMPT"]
    written = json.dumps([event.to_json_dict() for event in events], ensure_ascii=False)

    # 추적을 달아도 저장 결과는 정답 그대로
    assert differences(answer, result.after) + state_problems(result) == []
    # D1: 실행 시작 → 도구 14번 → 설명 1번 → 실행 끝, 모두 같은 실행 ID
    assert (kinds[0], kinds[-1]) == ("RUN_START", "RUN_END")
    assert kinds.count("RUN_START") == kinds.count("RUN_END") == 1
    assert [event.status for event in tools] == ["OK"] * 14
    assert [event.status for event in explanations] == ["OK"]
    assert events[-1].summary["outcome"] == "READY_FOR_REVIEW"
    assert {event.analysis_run_id for event in events} == {run.analysis_run_id}
    assert {event.trace_id for event in events} == {run.analysis_run_id}
    assert [event.seq for event in events] == sorted(event.seq for event in events)
    # D2: 위험 주문의 실제 주문 코드와 지연 선적 코드가 기록에 원문으로 없다
    orders = [code for code in codes if "-ORD-" in code]
    assert orders and len(codes) > len(orders)  # 주문 코드와 선적 코드가 모두 있음
    assert not [code for code in codes if code in written]


async def test_finalized_runs_link_from_detection_to_final_decision(db_session):
    """D3: 최종 선택까지 간 실행은 실행 ID 로 감지 이벤트부터 최종 선택까지 이어진다(NFR-007)."""
    db = db_session
    runs = (await db.execute(_FINALIZED)).all()
    if not runs:
        pytest.skip("최종 선택까지 간 실행이 없는 DB 라 건너뜀")
    drafts = 0
    for run in runs:
        links = (
            await db.execute(
                _LINKS,
                {
                    "id": run.analysis_run_id,
                    "seq": run.input_seq,
                    "snapshot": run.trigger_shipment_schedule_snapshot_id,
                    "parent": run.parent_analysis_run_id,
                },
            )
        ).one()
        explanation = run.final_explanation_json
        label = run.run_code

        # 입력 데이터 버전: 도메인 5개가 하나씩, 모두 실행 입력 순번 이하
        assert (links.versions, links.domains) == (5, 5), label
        # 결과 행 → 설명 → 최종 선택이 같은 실행 ID
        assert links.impacts > 0 and links.options > 0, label
        assert explanation["analysis_run_id"] == str(run.analysis_run_id), label
        assert (
            explanation["decision_signature_hash"] == run.decision_signature_sha256
        ), label
        assert links.decisions == 1, label  # 최종 선택의 대응안이 이 실행의 대응안
        # 감지 이벤트: 자동 실행은 시작 스냅샷이 그 지연의 선적 것, 재분석 실행은 부모 실행이 같은 지연
        if run.trigger_type == "AUTO":
            assert links.snapshot_shipment == run.shipment_id, label
        else:
            assert links.parent_event == run.delay_event_id, label
        drafts += links.drafts
    assert drafts >= 1  # 팀원 초안이 붙은 실행이 있다
