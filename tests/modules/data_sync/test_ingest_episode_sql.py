import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.modules.data_sync.application.ingest.ingest_service import DataIngestService
from app.modules.data_sync.infrastructure.data_sync_enums import (
    ConnectionMethod,
    DataSyncRunStatus,
    SourceDomain,
)
from app.modules.data_sync.infrastructure.repository.ingest_repository import (
    IngestRepository,
)
from app.modules.delay_analysis.application.detection.episode_rules import (
    EpisodeAction,
)
from app.modules.delay_analysis.infrastructure.analysis_enums import TriggerReason
from tests.modules.data_sync.test_ingest_service_sql import (
    RecordingStale,
    _company,
    _tms_shipment,
)

"""
    수집 적재 → 지연 에피소드 저장 연결 테스트 (실제 DB, 끝나면 롤백, docs/analysis/delay-detection.md 4절)
      - SHIPMENT 수집은 수집 완료 기록 뒤·STALE 판정 전에 같은 트랜잭션에서 에피소드를 열고 닫는다.
      - 초기 적재(고객사의 첫 SHIPMENT 수집, 결정 1-10)의 지연 스냅샷은 에피소드를 열지 않고, 다음 수집의 지연은 연다.
        확정 DB 는 모든 수집이 적재돼 있어서, 롤백 트랜잭션 안에서 고객사의 SHIPMENT 수집을 FAILED 로 바꿔
        "SHIPMENT 수집이 처음인 고객사"를 만든다.
"""

DAY1 = datetime(2026, 7, 2, 1, 0, tzinfo=UTC)
DAY2 = DAY1 + timedelta(days=1)


def _service(db, at) -> DataIngestService:
    return DataIngestService(db, IngestRepository(db), RecordingStale(), now=lambda: at)


async def _delayed_new_shipment(db, company_id):
    """새 선적(코드만 새로) + 예상 3PL 입고일을 계획일보다 30일 늦게. 계획일은 같은 출고일·경로의 기존 선적과 같다."""
    template = await _tms_shipment(db, company_id)
    planned = await db.scalar(
        text(
            "SELECT planned_3pl_inbound_date FROM shipments WHERE company_id = :c AND shipment_code = :s"
        ),
        {"c": company_id, "s": template.shipment_code},
    )
    return replace(
        template,
        shipment_code=f"TEST-{uuid.uuid7().hex[-10:]}",
        current_stage="AT_SEA",
        current_expected_3pl_inbound_date=planned + timedelta(days=30),
        actual_3pl_inbound_date=None,
    )


async def _events(db, company_id, shipment_code):
    return (
        await db.execute(
            text(
                """
                SELECT e.episode_no, e.stage, e.detected_at, e.closed_at, e.close_reason,
                       e.planned_3pl_inbound_date, e.expected_3pl_inbound_date
                  FROM delay_events e JOIN shipments s USING (shipment_id)
                 WHERE e.company_id = :c AND s.shipment_code = :s
                 ORDER BY e.episode_no
                """
            ),
            {"c": company_id, "s": shipment_code},
        )
    ).all()


async def _ingest(db, at, company_id, shipments):
    result = await _service(db, at).ingest(
        company_id, SourceDomain.SHIPMENT, ConnectionMethod.API, shipments
    )
    assert result.status == DataSyncRunStatus.SUCCEEDED, result.rejections
    return result


async def test_지연_선적이_들어오면_에피소드를_열고_입고되면_닫는다(db_write_session):
    db = db_write_session
    company_id = await _company(db)
    shipment = await _delayed_new_shipment(db, company_id)

    opened = await _ingest(db, DAY1, company_id, [shipment])
    received = await _ingest(
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

    assert [(o.action, o.trigger_reason) for o in opened.episode_outcomes] == [
        (EpisodeAction.START, TriggerReason.EPISODE_STARTED)
    ]
    assert [(o.action, o.trigger_reason) for o in received.episode_outcomes] == [
        (EpisodeAction.CLOSE_RECEIVED, None)
    ]
    assert (
        received.episode_outcomes[0].delay_event_id
        == opened.episode_outcomes[0].delay_event_id
    )
    [event] = await _events(db, company_id, shipment.shipment_code)
    assert (event.episode_no, event.stage, event.detected_at) == (1, "AT_SEA", DAY1)
    assert (event.closed_at, event.close_reason) == (DAY2, "RECEIVED")
    assert event.expected_3pl_inbound_date == shipment.current_expected_3pl_inbound_date


async def test_초기_적재의_지연_스냅샷은_에피소드를_열지_않고_다음_수집에서_연다(
    db_write_session,
):
    db = db_write_session
    company_id = await _company(db)
    shipment = await _delayed_new_shipment(db, company_id)
    await db.execute(
        text(
            """
            UPDATE data_sync_runs SET status = 'FAILED', applied_seq = NULL
             WHERE company_id = :c AND source_domain = 'SHIPMENT'
            """
        ),
        {"c": company_id},
    )  # 이 고객사의 SHIPMENT 수집이 처음인 상태

    initial = await _ingest(db, DAY1, company_id, [shipment])
    next_sync = await _ingest(
        db, DAY2, company_id, [replace(shipment, current_stage="AT_PORT")]
    )

    assert (
        initial.episode_outcomes == ()
    )  # 결정 1-10: 기준 스냅샷만으로는 지연 이벤트를 만들지 않음
    assert [(o.action, o.trigger_reason) for o in next_sync.episode_outcomes] == [
        (EpisodeAction.START, TriggerReason.EPISODE_STARTED)
    ]
    [event] = await _events(db, company_id, shipment.shipment_code)
    assert (event.episode_no, event.stage, event.detected_at) == (1, "AT_PORT", DAY2)


async def test_SHIPMENT_가_아닌_수집은_에피소드를_보지_않는다(db_write_session):
    db = db_write_session
    company_id = await _company(db)

    result = await _service(db, DAY1).ingest(
        company_id, SourceDomain.ORDER, ConnectionMethod.FILE, []
    )

    assert result.episode_outcomes == ()


async def test_수집은_판정_종료_STALE_판정_순서로_부른다(db_write_session):
    """회복 종료 전에 GLOW-18 이 이전 실행을 STALE 로 바꿀 수 있도록 판정과 종료가 나뉘어 있고,
    닫힌 에피소드의 실행이 STALE 판정 대상이 아니게 종료는 STALE 판정보다 먼저다 (delay-detection.md 3·4절)."""
    db = db_write_session
    company_id = await _company(db)
    calls: list[str] = []

    class OrderEpisodes:
        async def detect_after_sync(self, company_id, data_sync_run_id):
            calls.append("detect")
            return []

        async def close_episodes(self, company_id, outcomes):
            calls.append("close")

    class OrderStale:
        async def judge_after_sync(self, company_id, applied_seq, detected_at):
            calls.append("stale")
            return []

    service = DataIngestService(
        db,
        IngestRepository(db),
        OrderStale(),
        episode_service=OrderEpisodes(),
        now=lambda: DAY1,
    )
    await service.ingest(company_id, SourceDomain.SHIPMENT, ConnectionMethod.API, [])

    assert calls == ["detect", "close", "stale"]
