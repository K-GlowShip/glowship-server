import logging
import uuid
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.locks import lock_company
from app.core.rls_context import set_rls_context
from app.core.security.tenant import TenantContext
from app.modules.data_sync.application.exception.data_sync_exceptions import (
    SourceNotSupportedException,
    SyncInProgressException,
)
from app.modules.data_sync.application.ingest.erp_wms_parser import (
    parse_erp_orders,
    parse_wms_inventory,
)
from app.modules.data_sync.application.ingest.ingest_service import (
    DataIngestService,
    IngestResult,
    StartedRun,
)
from app.modules.data_sync.application.ingest.tms_parser import parse_tms_shipments
from app.modules.data_sync.application.virtual_clock import (
    TICK_ORDER,
    VIRTUAL_FINISH_AFTER,
    SyncClock,
    SyncWindow,
    TickPlan,
    next_midnight,
    plan_tick,
)
from app.modules.data_sync.infrastructure.data_sync_enums import (
    ConnectionMethod,
    SourceDomain,
)
from app.modules.data_sync.infrastructure.repository.data_sync_run_repository import (
    DataSyncRunRepository,
)
from app.modules.data_sync.infrastructure.repository.ingest_repository import (
    IngestRepository,
)
from app.modules.data_sync.infrastructure.virtual_sources import VirtualSourceScenario
from app.modules.data_sync.infrastructure.virtual_tms import VirtualTmsScenario
from app.modules.delay_analysis.application.stale.stale_service import (
    StaleJudgmentService,
)
from app.modules.delay_analysis.infrastructure.repository.stale_change_repository import (
    StaleChangeRepository,
)

"""
    수동 수집 (API 25 POST /data-sync-runs, 명세 v6.2 4.3·D16, GLOW-13·49)
      - request(요청 안): SHIPMENT 만 받는다(2026-11-05 결정, 나머지는 422). L1 을 잡고 가상 시각 구간(virtual_clock)을 정한 뒤
        같은 트랜잭션에서 수집 서비스 start 로 RUNNING 을 커밋한다. 같은 원천에 RUNNING 이 있으면 409 SYNC_IN_PROGRESS.
        라우터는 이 결과로 202 와 실행 ID 를 응답한다.
      - run(응답 뒤 백그라운드): 새 세션에서 가상 TMS 시나리오의 (지난 수집 시작, 이번 수집 시각] 응답을 받아
        파싱하고 apply 한다. 응답을 받기 전에 실패하면 fail 로 FAILED 를 남긴다(RUNNING 으로 남으면 다음 수집이 막힘).
        백그라운드라 예외를 받을 호출 측이 없으므로 로그로 남기고, 결과는 화면이 GET /data-sync-runs/latest 로 본다.
      - 세션은 워커 역할 전용 세션(open_worker_session)이다. 요청 세션(get_db)과 따로이며 수집 서비스가 직접 커밋한다.
      - 가상 시각이면 수집 서비스 now 를 이번 수집 시각으로 고정한다. 수집 시작·일정 스냅샷 관측 시각이 이 값이고,
        완료 시각은 시작 + 30초다(이슈 #39 규칙 3). 실제 시각이면 서버의 지금 시각을 그대로 쓴다.
      - 적용이 커밋된 뒤 이 수집이 만든 AUTO 분석 실행(created_run_ids, GLOW-18)을 start_runs 로 넘긴다(GLOW-128 자동 시작).
        넘기는 함수가 예외를 내도 수집 결과는 그대로다(이미 커밋됨). 실행은 DETECTED 로 남아 다음 서버 시작 정리가 시작한다.
"""

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
# 커밋된 수집이 만든 분석 실행을 넘겨받는다(고객사, 실행 ID 들). 분석 실행기 start_many 와 같은 모양
RunStarter = Callable[[uuid.UUID, Sequence[uuid.UUID]], object]


@dataclass(frozen=True)
class ManualSyncTicket:
    """request 가 시작한 수집. run 에 넘긴다."""

    started: StartedRun
    window: SyncWindow
    company_code: str


class ManualSyncService:
    def __init__(
        self,
        open_session: SessionFactory,
        virtual_start: datetime | None,
        load_scenario: Callable[[], VirtualTmsScenario | None],
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        load_sources: Callable[[], VirtualSourceScenario | None] = lambda: None,
        start_runs: RunStarter | None = None,
    ):
        self.open_session = open_session
        self.virtual_start = virtual_start
        self.load_scenario = load_scenario
        self.now = now
        # 고객사 tick(다섯 원천)이 읽는 가상 원천 응답 (가상 TMS + 가상 ERP·WMS 파일)
        self.load_sources = load_sources
        self.start_runs = start_runs

    def _ingest(self, db: AsyncSession, at: datetime) -> DataIngestService:
        stale = StaleJudgmentService(db, StaleChangeRepository(db))
        if self.virtual_start is None:
            # 실제 시각: 시작·완료 모두 지금 시각 (완료는 시작보다 늦게, 이슈 #39 규칙 3)
            return DataIngestService(db, IngestRepository(db), stale, now=self.now)
        return DataIngestService(
            db,
            IngestRepository(db),
            stale,
            now=lambda: at,
            finish_after=VIRTUAL_FINISH_AFTER,
        )

    async def request(
        self, tenant: TenantContext, source_domain: SourceDomain
    ) -> ManualSyncTicket:
        if source_domain != SourceDomain.SHIPMENT:
            raise SourceNotSupportedException()
        company_id = tenant.company_id
        async with self.open_session() as db:
            await set_rls_context(db, user_id=None, company_id=company_id)
            # 수집 구간은 L1 안에서 읽는다(이슈 #39 댓글, PR #40 리뷰). 같은 트랜잭션에서 start 가 L1 을 다시 잡고
            # (같은 세션의 advisory 잠금이라 기다리지 않음) RUNNING 을 커밋할 때 잠금이 풀린다. 그래서 버튼을 빠르게 두 번 눌러도
            # 두 번째 요청은 첫 수집이 RUNNING 이면 409, 이미 적재됐으면 다음 날 구간을 받는다(같은 날 구간을 두 번 받지 않음).
            await lock_company(db, company_id)
            runs = DataSyncRunRepository(db)
            company_code = await runs.find_company_code(company_id)
            window = await SyncClock(runs, self.virtual_start, self.now).next_window(
                company_id, source_domain
            )
            started = await self._ingest(db, window.at).start(
                company_id, source_domain, ConnectionMethod.API
            )
        return ManualSyncTicket(
            started=started, window=window, company_code=company_code or ""
        )

    async def run(self, ticket: ManualSyncTicket) -> IngestResult | None:
        """원천 응답을 받아 적용합니다. 실패하면 FAILED 를 남기고 로그를 쓴 뒤 None 을 돌려줍니다."""
        run_id = ticket.started.data_sync_run_id
        async with self.open_session() as db:
            ingest = self._ingest(db, ticket.window.at)
            try:
                scenario = self.load_scenario()
                payloads = (
                    scenario.shipments_between(
                        ticket.company_code, ticket.window.after, ticket.window.at
                    )
                    if scenario is not None
                    else []
                )
                parsed = parse_tms_shipments(payloads)
            except Exception as error:
                await ingest.fail(ticket.started, error)
                logger.exception("수동 수집 %s: 원천 응답을 받지 못해 FAILED", run_id)
                return None
            try:
                result = await ingest.apply(
                    ticket.started, parsed.shipments, parsed.rejections
                )
            except Exception:
                # apply 가 이미 되돌리고 FAILED 를 남겼다
                logger.exception("수동 수집 %s: 적용 실패", run_id)
                return None
        self._start_created_runs(ticket.started.company_id, result, run_id)
        return result

    # --- 고객사 tick: 동기화 버튼 한 번에 다섯 원천을 함께 (2026-11-09 결정) ---------------------------------------

    async def request_tick(self, tenant: TenantContext) -> TickTicket:
        """
        다음 tick 을 정하고 다섯 원천 수집을 RUNNING 으로 남긴다(요청 안). L1 안에서 tick 을 정해 같은 tick 을 두 번
        받지 않는다. 그 고객사에 RUNNING 수집이 하나라도 있으면 아무것도 시작하지 않고 409 SYNC_IN_PROGRESS.
        """
        company_id = tenant.company_id
        async with self.open_session() as db:
            await set_rls_context(db, user_id=None, company_id=company_id)
            await lock_company(db, company_id)
            runs = DataSyncRunRepository(db)
            ingest_repo = IngestRepository(db)
            for domain in TICK_ORDER:
                if await ingest_repo.has_running_run(company_id, domain):
                    await db.rollback()
                    raise SyncInProgressException()
            company_code = await runs.find_company_code(company_id)
            plan = await plan_tick(runs, company_id, self.virtual_start, self.now)
            started: list[StartedRun] = []
            try:
                for domain in TICK_ORDER:
                    # start 는 커밋하며 L1 을 놓고, 다음 원천의 start 가 다시 잡는다
                    started.append(
                        await self._ingest(db, plan.windows[domain].at).start(
                            company_id, domain, ConnectionMethod.API
                        )
                    )
            except Exception as error:
                # 사이에 다른 요청이 끼어들거나(409) DB 오류 등으로 시작이 멈추면 이미 시작한 것을 FAILED 로 닫는다.
                # RUNNING 으로 남으면 그 고객사의 동기화가 모두 409 로 막힌다(PR #91 리뷰). fail 이 먼저 되돌린다.
                for run in started:
                    await self._ingest(db, plan.windows[run.source_domain].at).fail(
                        run, error
                    )
                raise
        return TickTicket(
            plan=plan, started=tuple(started), company_code=company_code or ""
        )

    async def run_tick(self, ticket: TickTicket) -> list[IngestResult | None]:
        """
        tick 의 다섯 원천을 순서대로 적용한다(백그라운드). 원천마다 자기 트랜잭션이라 하나가 실패해도 나머지는 계속하고,
        실패한 원천은 다음 tick 의 구간이 넓어져 빠진 응답을 받는다(virtual_clock.tick_window).
        """
        try:
            sources = self.load_sources()
        except Exception as error:
            async with self.open_session() as db:
                for run in ticket.started:
                    await self._ingest(
                        db, ticket.plan.windows[run.source_domain].at
                    ).fail(run, error)
            logger.exception(
                "동기화 tick %s: 가상 원천 파일을 읽지 못해 FAILED", ticket.plan.at
            )
            return [None] * len(ticket.started)
        return [await self._apply_one(ticket, run, sources) for run in ticket.started]

    async def _apply_one(
        self,
        ticket: TickTicket,
        run: StartedRun,
        sources: VirtualSourceScenario | None,
    ) -> IngestResult | None:
        window = ticket.plan.windows[run.source_domain]
        async with self.open_session() as db:
            ingest = self._ingest(db, window.at)
            try:
                payloads = (
                    sources.records_between(
                        ticket.company_code, run.source_domain, window.after, window.at
                    )
                    if sources is not None
                    else []
                )
                records, rejections = _parse(run.source_domain, payloads)
            except Exception as error:
                await ingest.fail(run, error)
                logger.exception(
                    "동기화 %s %s: 원천 응답을 받지 못해 FAILED",
                    run.source_domain,
                    window.at,
                )
                return None
            try:
                result = await ingest.apply(run, records, rejections)
            except Exception:
                logger.exception(
                    "동기화 %s %s: 적용 실패", run.source_domain, window.at
                )
                return None
        self._start_created_runs(run.company_id, result, run.data_sync_run_id)
        return result

    def _start_created_runs(
        self, company_id: uuid.UUID, result: IngestResult, run_id: object
    ) -> None:
        """커밋된 수집이 만든 AUTO 분석 실행을 분석 실행기에 넘긴다(GLOW-128). 실패해도 수집 결과는 그대로(DETECTED 로 남음)"""
        if not result.created_run_ids or self.start_runs is None:
            return
        try:
            self.start_runs(company_id, result.created_run_ids)
        except Exception:
            logger.exception(
                "수동 수집 %s: 분석 실행 %s 시작 실패(DETECTED 로 남음)",
                run_id,
                list(result.created_run_ids),
            )

    async def run_until_midnight(self, tenant: TenantContext, first: TickTicket) -> int:
        """
        데모의 다음 날로: 첫 tick 을 적용한 뒤 다음 자정 tick 까지 이어서 진행한다. 진행한 tick 수.
        한 tick 의 다섯 원천이 모두 실패하면 멈춘다. 다음 tick 시각은 마지막 적재 수집 기준(next_tick_at)이라
        전부 실패한 tick 뒤에는 같은 tick 을 다시 정해 끝나지 않기 때문이다(PR #91 리뷰). 빠진 구간은 다음 누름이 다시 받는다.
        """
        target = next_midnight(first.plan.at)
        ticket = first
        count = 1
        if not any(await self.run_tick(ticket)):
            return count
        while ticket.plan.at < target:
            ticket = await self.request_tick(tenant)
            count += 1
            if not any(await self.run_tick(ticket)):
                logger.warning(
                    "다음 날로 %s: tick 의 원천이 모두 실패해 멈춤", ticket.plan.at
                )
                break
        return count


@dataclass(frozen=True)
class TickTicket:
    """request_tick 이 시작한 다섯 원천 수집. run_tick 에 넘긴다."""

    plan: TickPlan
    started: tuple[StartedRun, ...]
    company_code: str


def _parse(domain: SourceDomain, payloads: list) -> tuple[list, list]:
    """원천별 파서. 상품·창고는 지금 가상 응답이 없어 빈 목록(마스터 변경 없음)"""
    if domain == SourceDomain.SHIPMENT:
        parsed = parse_tms_shipments(payloads)
        return parsed.shipments, parsed.rejections
    if domain == SourceDomain.ORDER:
        parsed = parse_erp_orders(payloads)
        return parsed.records, parsed.rejections
    if domain == SourceDomain.INVENTORY:
        parsed = parse_wms_inventory(payloads)
        return parsed.records, parsed.rejections
    return [], []
