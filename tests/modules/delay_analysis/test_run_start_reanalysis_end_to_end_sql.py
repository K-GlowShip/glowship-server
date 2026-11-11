import json
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.core.security.tenant import TenantContext
from app.modules.data_sync.infrastructure.repository.data_sync_run_repository import (
    DataSyncRunRepository,
)
from app.modules.decision.application.decision_clock import DecisionClock
from app.modules.decision.application.exception.decision_exceptions import (
    DecisionErrorCode,
    DecisionException,
)
from app.modules.decision.application.final_decision_service import (
    FinalDecisionService,
)
from app.modules.decision.application.member_draft_service import MemberDraftService
from app.modules.decision.application.reanalysis_service import ReanalysisService
from app.modules.decision.infrastructure.repository.final_decision_repository import (
    FinalDecisionRepository,
)
from app.modules.decision.infrastructure.repository.member_draft_repository import (
    MemberDraftRepository,
)
from app.modules.delay_analysis.application.run_start.app_runner import (
    build_app_runner,
)
from app.modules.delay_analysis.infrastructure.agent_trace_outbox import (
    OUTBOX_DIR_NAME,
)
from tests.modules.delay_analysis.test_demo_seed_sql import (
    VIRTUAL_START,
    _ready,
    _seed,
)

"""
    재분석 → 분석 → 그 실행에서 초안·최종 선택 끝까지 (이슈 #51 본문 시험) — 실제 DB·실제 실행 묶음, 끝나면 롤백
      - 확정 데이터는 지연이 모두 닫혀 재분석·초안이 EPISODE_CLOSED 다. 데모 시드(scripts/seed_demo_runs.py)로 최신 순번에서
        열린 지연을 만들고, 그 READY 실행을 재분석한다. 시드 결과(엔진 결과를 바로 넣는 임시 경로)와 실행기 결과(에이전트 도구 경로)가
        같은 입력 순번에서 같은 대응안을 내는지가 독립 기대값이다(시드를 서버 저장 경로로 바꾸면(이슈 #70) 이 대조는 독립이 아님).
      - 실행기는 앱 조립(build_app_runner: 가상 시각·저장 시각·추적기)을 그대로 쓰고 세션만 시험 세션으로 바꾼다.
      - 확인: ① 재분석 직후 멈춤(새 실행 DETECTED·초안 RUN_NOT_READY·재분석 RUN_IN_PROGRESS, 이전 실행 NOT_CURRENT_RUN)
        ② 실행기가 꺼져 있으면 그대로 ③ 분석 뒤 READY_FOR_REVIEW·설명 PASSED·데이터 버전 0 → 5·대응안 = 시드
        ④ 그 실행에서 초안·최종 선택 → FINALIZED ⑤ 추적 내부 로그에 그 실행의 처음부터 끝까지(원래 주문 코드 없음).
      - 재분석 실행의 입력 순번이 최신이라 저장 직후 재판정에 걸릴 변경이 없다(판정 서명 재계산이 돌지 않음).
        최종 선택 뒤 동기화 → STALE 판정은 이 시험 범위 밖이다(사용자 결정 11-11).
      - GLOWSHIP_ENGINE_DIR 이 없거나 확정 데이터(마지막 SHIPMENT 수집 07-01)가 아니면 건너뛴다(데모 시드 시험과 같은 조건).
      - 시드(25~35초)를 한 번만 돌리려고 시험 함수 하나에 단계마다 확인한다.
"""

_TARGET = text(
    """
    SELECT r.company_id, r.analysis_run_id, r.input_seq,
           (SELECT u.user_id FROM users u WHERE u.company_id = r.company_id
             AND u.role_code = 'CLIENT_MEMBER' ORDER BY u.user_id LIMIT 1) AS member_id,
           (SELECT u.user_id FROM users u WHERE u.company_id = r.company_id
             AND u.role_code = 'CLIENT_LEAD' ORDER BY u.user_id LIMIT 1) AS lead_id
      FROM analysis_runs r
      JOIN delay_events e USING (delay_event_id)
      JOIN shipments s ON s.shipment_id = e.shipment_id
     WHERE s.shipment_code = 'DEMO-0702-PORT' AND r.is_latest
    """
)
_RUN = text(
    "SELECT status, completed_status, explanation_validation_status, input_seq, is_latest,"
    " started_at, finished_at FROM analysis_runs WHERE analysis_run_id = :r"
)
_OPTIONS = text(
    "SELECT option_type, feasibility_status, is_recommended FROM response_options"
    " WHERE analysis_run_id = :r ORDER BY option_type"
)
_DATA_VERSIONS = text(
    "SELECT count(*) FROM analysis_run_data_versions WHERE analysis_run_id = :r"
)
_FEASIBLE = text(
    "SELECT response_option_id FROM response_options WHERE analysis_run_id = :r"
    " AND feasibility_status = 'FEASIBLE' ORDER BY option_type LIMIT 1"
)


async def _rejected(call) -> DecisionErrorCode:
    with pytest.raises(DecisionException) as raised:
        await call
    return raised.value.error_code


def _events(trace_dir, run_id) -> list[dict]:
    lines = [
        json.loads(line)
        for path in (trace_dir / OUTBOX_DIR_NAME).rglob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    return sorted(
        (e for e in lines if e["analysis_run_id"] == str(run_id)),
        key=lambda e: e["seq"],
    )


async def test_reanalysis_is_analyzed_and_then_drafted_and_decided(
    db_write_session, tmp_path
):
    db = db_write_session
    await _ready(db)
    await _seed(db)
    await db.commit()
    v1 = (await db.execute(_TARGET)).one()
    if v1.member_id is None or v1.lead_id is None:
        pytest.skip("데모 고객사에 팀원·팀장이 없는 DB")
    member = TenantContext(user_id=v1.member_id, company_id=v1.company_id)
    lead = TenantContext(user_id=v1.lead_id, company_id=v1.company_id)
    clock = DecisionClock(DataSyncRunRepository(db), virtual=True)
    drafts = MemberDraftService(MemberDraftRepository(db), clock)
    seeded_options = (await db.execute(_OPTIONS, {"r": v1.analysis_run_id})).all()

    def draft(run_id, option_id=None):
        return drafts.save_draft(
            member,
            analysis_run_id=run_id,
            response_option_id=option_id,
            opinion_text=None if option_id else "의견",
        )

    def reanalyze(run_id):
        return ReanalysisService(db, MemberDraftRepository(db), clock).request(
            member, analysis_run_id=run_id, request_reason="끝까지 시험"
        )

    # ① 재분석 직후: 새 실행은 DETECTED 로 멈춰 있고 그 지연에서 판단을 이어 갈 수 없다(이슈 #51 본문)
    v2 = await reanalyze(v1.analysis_run_id)
    created = (await db.execute(_RUN, {"r": v2.run_id})).one()
    assert (created.status, created.is_latest, created.input_seq) == (
        "DETECTED",
        True,
        v1.input_seq,
    )
    assert await db.scalar(_DATA_VERSIONS, {"r": v2.run_id}) == 0
    assert await _rejected(draft(v2.run_id)) == DecisionErrorCode.RUN_NOT_READY
    assert await _rejected(reanalyze(v2.run_id)) == DecisionErrorCode.RUN_IN_PROGRESS
    assert (
        await _rejected(draft(v1.analysis_run_id)) == DecisionErrorCode.NOT_CURRENT_RUN
    )
    await db.rollback()

    @asynccontextmanager
    async def same_session():
        yield db

    settings = get_settings().model_copy(
        update={"virtual_clock_start": VIRTUAL_START, "agent_trace_dir": tmp_path}
    )

    # ② 음성 대조: 실행기가 꺼져 있으면 시작하지 않고 그대로 멈춰 있다
    off = build_app_runner(
        settings.model_copy(update={"analysis_auto_start_enabled": False}),
        open_session=same_session,
    )
    assert off.start(v1.company_id, v2.run_id) is False
    assert (await db.execute(_RUN, {"r": v2.run_id})).one().status == "DETECTED"

    # ③ 앱 실행기(실제 묶음)로 분석
    runner = build_app_runner(
        settings.model_copy(update={"analysis_auto_start_enabled": True}),
        open_session=same_session,
    )
    assert runner.start(v1.company_id, v2.run_id) is True
    await runner.wait_idle()
    done = (await db.execute(_RUN, {"r": v2.run_id})).one()
    assert (done.status, done.completed_status, done.explanation_validation_status) == (
        "READY_FOR_REVIEW",
        "READY_FOR_REVIEW",
        "PASSED",
    )
    assert done.finished_at == done.started_at + timedelta(seconds=1)
    assert await db.scalar(_DATA_VERSIONS, {"r": v2.run_id}) == 5
    assert (await db.execute(_OPTIONS, {"r": v2.run_id})).all() == seeded_options

    # ④ 그 실행에서 초안·최종 선택, 이전 실행은 여전히 현재 실행이 아님
    option_id = await db.scalar(_FEASIBLE, {"r": v2.run_id})
    assert option_id is not None
    await draft(v2.run_id, option_id)
    assert (
        await _rejected(draft(v1.analysis_run_id)) == DecisionErrorCode.NOT_CURRENT_RUN
    )
    await FinalDecisionService(db, FinalDecisionRepository(db), clock).decide(
        lead,
        analysis_run_id=v2.run_id,
        response_option_id=option_id,
        reason_text="재고 영향이 가장 적음",
        idempotency_key="reanalysis-end-to-end-1",
    )
    assert (await db.execute(_RUN, {"r": v2.run_id})).one().status == "FINALIZED"

    # ⑤ 추적 내부 로그: 그 실행의 처음부터 끝까지, 원래 주문 코드 없음
    events = _events(tmp_path, v2.run_id)
    kinds = [e["kind"] for e in events]
    assert kinds[0] == "RUN_START" and kinds[-1] == "RUN_END"
    assert events[-1]["summary"]["outcome"] == "READY_FOR_REVIEW"
    assert "TOOL_ATTEMPT" in kinds and "EXPLANATION_ATTEMPT" in kinds
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert all("-ORD-" not in json.dumps(e, ensure_ascii=False) for e in events)
