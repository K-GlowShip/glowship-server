import uuid
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, time

import pytest
from sqlalchemy import text

from app.modules.data_sync.application.ingest.ingest_service import DataIngestService
from app.modules.data_sync.application.ingest.source_payload import (
    SourceShipment,
    SourceShipmentLine,
)
from app.modules.data_sync.infrastructure.data_sync_enums import (
    ConnectionMethod,
    DataSyncRunStatus,
    SourceDomain,
)
from app.modules.data_sync.infrastructure.repository.ingest_repository import (
    IngestRepository,
)

"""
    시간순 수집 재현 테스트 (실제 DB, 끝나면 롤백)
      - 확정 2026 상반기 선적의 관측 이력(일정 스냅샷)을 날짜 순서대로 TMS 원천 응답으로 다시 만들어,
        이름만 바꾼 새 선적(R-{원래 코드})으로 수집 서비스에 넣습니다.
      - 재현한 일정 스냅샷이 원래 스냅샷과 날짜·단계·예상일까지 같은지, 최종 선적 행이 같은지 확인합니다.
      - 관측 원천: 초기 적재(첫 수집일)에 들어온 선적은 그날 값, 이후는 스냅샷이 있는 날의 값.
        스냅샷이 없는 선적(초기 적재 때 이미 RECEIVED)은 첫 수집일에 최종 값으로 한 번만 보냅니다 (결정 1-10).
      - STALE 판정은 대역을 씁니다 (재현한 선적 변경이 실제 실행을 건드리지 않게).
      - 지연 에피소드 저장(GLOW-16)도 함께 돌아, 재현 선적의 delay_events 가 원래 선적의 것과 같고(번호·단계·계획·예상일·
        감지·종료 날짜·종료 사유) 판정이 낸 AUTO 실행 생성 사유가 확정 DB 의 AUTO 실행과 같은지 확인합니다.
        재현은 이미 SHIPMENT 수집이 있는 고객사에 넣으므로 초기 적재 예외는 적용되지 않습니다. 확정 데이터의 초기 적재
        스냅샷에는 지연이 없어 결과는 같습니다 (docs/analysis/delay-detection.md 4절).
"""

PREFIX = "R-"
OBSERVED_TIME = time(3, 20)


class NoStale:
    async def judge_after_sync(self, company_id, applied_seq, detected_at):
        return []


class ReplayRepository(IngestRepository):
    """
    지난 날짜를 그 날짜 시각으로 다시 적재하기 위한 대역. 서비스는 시작 시각을 "고객사의 마지막 적재 수집 시작 + 1초"
    보다 늦게 확정하므로(이슈 #39 규칙 5) 그대로 두면 재현 수집이 모두 07-01 뒤로 밀린다. 이 시험은 시각 규칙이 아니라
    관측 이력 재현을 보므로 마지막 적재 수집을 없는 것으로 본다(시작 초 겹침 확인은 그대로).
    """

    async def last_applied_started_at(self, company_id):
        return None


async def _first_insert_day(db, company_id) -> dict[uuid.UUID, date]:
    rows = await db.execute(
        text(
            "SELECT c.entity_ref, min(r.started_at)::date AS day"
            " FROM data_change_records c JOIN data_sync_runs r USING (data_sync_run_id)"
            " WHERE c.company_id = :c AND c.entity_type = 'SHIPMENT' AND c.change_type = 'INSERT'"
            " GROUP BY c.entity_ref"
        ),
        {"c": company_id},
    )
    return {row.entity_ref: row.day for row in rows}


async def _snapshots(db, company_id, shipment_ids) -> dict[uuid.UUID, list]:
    rows = await db.execute(
        text(
            "SELECT shipment_id, observed_at, stage, expected_port_arrival_date,"
            " expected_3pl_inbound_date FROM shipment_schedule_snapshots"
            " WHERE company_id = :c AND shipment_id = ANY(:ids) ORDER BY observed_at"
        ),
        {"c": company_id, "ids": list(shipment_ids)},
    )
    out: defaultdict[uuid.UUID, list] = defaultdict(list)
    for row in rows:
        out[row.shipment_id].append(row)
    return out


def _summary(snapshot) -> tuple:
    return (
        snapshot.observed_at.date(),
        snapshot.stage,
        snapshot.expected_port_arrival_date,
        snapshot.expected_3pl_inbound_date,
    )


async def test_time_ordered_tms_responses_reproduce_schedule_snapshots(
    db_write_session,
):
    db = db_write_session
    repo = ReplayRepository(db)
    companies = list(
        await db.scalars(text("SELECT DISTINCT company_id FROM shipments"))
    )
    if not companies:
        pytest.skip("선적 데이터가 없는 DB")

    reproduced_total = original_total = 0
    reasons_total: Counter = Counter()
    for company_id in companies:
        book = await repo.load_code_book(company_id, SourceDomain.SHIPMENT)
        tms_sku = {code.sku_id: external for external, code in book.skus.items()}
        tms_warehouse = {
            c.warehouse_id: external for external, c in book.warehouses.items()
        }
        codes = list(
            await db.scalars(
                text("SELECT shipment_code FROM shipments WHERE company_id = :c"),
                {"c": company_id},
            )
        )
        final = await repo.load_shipments(company_id, codes)
        by_id = {s.row_id: s.record for s in final.values()}
        first_day = await _first_insert_day(db, company_id)
        history = await _snapshots(db, company_id, by_id)
        load_day = min(first_day.values())

        # 날짜별 관측: (선적 ID, 단계, 예상 항구 도착일, 예상 3PL 입고일)
        observations: defaultdict[date, list] = defaultdict(list)
        for shipment_id, record in by_id.items():
            snaps = history.get(shipment_id, [])
            if not snaps:
                observations[first_day[shipment_id]].append(
                    (
                        shipment_id,
                        record.current_stage,
                        record.current_expected_port_arrival_date,
                        record.current_expected_3pl_inbound_date,
                    )
                )
            for snap in snaps:
                observations[snap.observed_at.date()].append(
                    (
                        shipment_id,
                        snap.stage,
                        snap.expected_port_arrival_date,
                        snap.expected_3pl_inbound_date,
                    )
                )
        assert min(observations) == load_day

        for day in sorted(observations):
            at = datetime.combine(day, OBSERVED_TIME, tzinfo=UTC)
            batch = []
            for shipment_id, stage, eta_port, eta_3pl in observations[day]:
                record = by_id[shipment_id]
                batch.append(
                    SourceShipment(
                        shipment_code=PREFIX + record.shipment_code,
                        transport_mode=record.transport_mode,
                        warehouse_code=tms_warehouse[record.warehouse_id],
                        planned_kr_release_date=record.planned_kr_release_date,
                        current_stage=stage,
                        current_expected_port_arrival_date=eta_port,
                        current_expected_3pl_inbound_date=eta_3pl,
                        actual_3pl_inbound_date=record.actual_3pl_inbound_date
                        if stage == "RECEIVED"
                        else None,
                        lines=tuple(
                            SourceShipmentLine(
                                line.line_no,
                                tms_sku[line.sku_id],
                                line.shipped_quantity,
                            )
                            for line in record.lines
                        ),
                    )
                )
            service = DataIngestService(db, repo, NoStale(), now=lambda at=at: at)
            result = await service.ingest(
                company_id, SourceDomain.SHIPMENT, ConnectionMethod.API, batch
            )
            assert result.status == DataSyncRunStatus.SUCCEEDED, (
                day,
                result.rejections,
            )
            reasons_total.update(
                o.trigger_reason for o in result.episode_outcomes if o.trigger_reason
            )

        # 최종 선적 행이 같다 (코드만 다름)
        replayed = await repo.load_shipments(
            company_id, [PREFIX + code for code in codes]
        )
        assert len(replayed) == len(codes)
        replayed_ids = {}
        for code in codes:
            again = replayed[PREFIX + code]
            original = final[code].record
            assert again.record.shipment_code == PREFIX + code
            assert {
                name: getattr(again.record, name) for name in type(original).FIELDS
            } == {name: getattr(original, name) for name in type(original).FIELDS}, code
            assert again.record.lines == original.lines, code
            replayed_ids[final[code].row_id] = again.row_id

        # 일정 스냅샷이 같다 (날짜·단계·예상일, 순서)
        again_history = await _snapshots(db, company_id, replayed_ids.values())
        for original_id, again_id in replayed_ids.items():
            original = [_summary(s) for s in history.get(original_id, [])]
            again = [_summary(s) for s in again_history.get(again_id, [])]
            assert again == original, by_id[original_id].shipment_code
            original_total += len(original)
            reproduced_total += len(again)

        # 지연 에피소드가 같다 (선적 코드만 다름, 시각은 재현 관측 시각이라 날짜로 비교)
        for original_id, again_id in replayed_ids.items():
            assert await _episodes(db, again_id) == await _episodes(db, original_id), (
                by_id[original_id].shipment_code
            )

    assert reproduced_total == original_total
    original_reasons = Counter(
        dict(
            (
                await db.execute(
                    text(
                        "SELECT trigger_reason, COUNT(*) FROM analysis_runs"
                        " WHERE trigger_type = 'AUTO' GROUP BY trigger_reason"
                    )
                )
            ).all()
        )
    )
    assert (
        reasons_total == original_reasons
    )  # 확정 DB: EPISODE_STARTED 8·STAGE_CHANGED 8·SCHEDULE_WORSENED 2·DELAY_RECOVERED 1
    # 재현 전 확정 데이터(선적 166건)라면 스냅샷은 223건
    if len(codes_total := await _original_codes(db)) == 166:
        assert original_total == 223, len(codes_total)


async def _episodes(db, shipment_id) -> list[tuple]:
    rows = await db.execute(
        text(
            "SELECT episode_no, stage, planned_3pl_inbound_date, expected_3pl_inbound_date,"
            " detected_at, closed_at, close_reason FROM delay_events"
            " WHERE shipment_id = :s ORDER BY episode_no"
        ),
        {"s": shipment_id},
    )
    return [
        (
            r.episode_no,
            r.stage,
            r.planned_3pl_inbound_date,
            r.expected_3pl_inbound_date,
            r.detected_at.date(),
            r.closed_at.date() if r.closed_at else None,
            r.close_reason,
        )
        for r in rows
    ]


async def _original_codes(db) -> list[str]:
    return list(
        await db.scalars(
            text("SELECT shipment_code FROM shipments WHERE shipment_code NOT LIKE :p"),
            {"p": PREFIX + "%"},
        )
    )
