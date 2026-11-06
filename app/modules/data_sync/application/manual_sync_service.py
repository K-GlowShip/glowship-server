import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rls_context import set_rls_context
from app.core.security.tenant import TenantContext
from app.modules.data_sync.application.exception.data_sync_exceptions import (
    SourceNotSupportedException,
)
from app.modules.data_sync.application.ingest.ingest_service import (
    DataIngestService,
    IngestResult,
    StartedRun,
)
from app.modules.data_sync.application.ingest.tms_parser import parse_tms_shipments
from app.modules.data_sync.application.virtual_clock import (
    VIRTUAL_FINISH_AFTER,
    SyncClock,
    SyncWindow,
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
from app.modules.data_sync.infrastructure.virtual_tms import VirtualTmsScenario
from app.modules.delay_analysis.application.stale.stale_service import (
    StaleJudgmentService,
)
from app.modules.delay_analysis.infrastructure.repository.stale_change_repository import (
    StaleChangeRepository,
)

"""
    수동 수집 (API 25 POST /data-sync-runs, 명세 v6.2 4.3·D16, GLOW-13·49)
      - request(요청 안): SHIPMENT 만 받는다(2026-11-05 결정, 나머지는 422). 가상 시각 구간(virtual_clock)을 정하고
        수집 서비스 start 로 RUNNING 을 커밋한다. 같은 원천에 RUNNING 이 있으면 409 SYNC_IN_PROGRESS.
        라우터는 이 결과로 202 와 실행 ID 를 응답한다.
      - run(응답 뒤 백그라운드): 새 세션에서 가상 TMS 시나리오의 (지난 수집 시작, 이번 수집 시각] 응답을 받아
        파싱하고 apply 한다. 응답을 받기 전에 실패하면 fail 로 FAILED 를 남긴다(RUNNING 으로 남으면 다음 수집이 막힘).
        백그라운드라 예외를 받을 호출 측이 없으므로 로그로 남기고, 결과는 화면이 GET /data-sync-runs/latest 로 본다.
      - 세션은 워커 역할 전용 세션(open_worker_session)이다. 요청 세션(get_db)과 따로이며 수집 서비스가 직접 커밋한다.
      - 가상 시각이면 수집 서비스 now 를 이번 수집 시각으로 고정한다. 수집 시작·일정 스냅샷 관측 시각이 이 값이고,
        완료 시각은 시작 + 30초다(이슈 #39 규칙 3). 실제 시각이면 서버의 지금 시각을 그대로 쓴다.
"""

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


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
    ):
        self.open_session = open_session
        self.virtual_start = virtual_start
        self.load_scenario = load_scenario
        self.now = now

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
            runs = DataSyncRunRepository(db)
            company_code = await runs.find_company_code(company_id)
            window = await SyncClock(runs, self.virtual_start, self.now).next_window(
                company_id, source_domain
            )
            await (
                db.rollback()
            )  # 읽기 트랜잭션을 닫고 start 가 새 트랜잭션에서 잠금·기록
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
                return await ingest.apply(
                    ticket.started, parsed.shipments, parsed.rejections
                )
            except Exception:
                # apply 가 이미 되돌리고 FAILED 를 남겼다
                logger.exception("수동 수집 %s: 적용 실패", run_id)
                return None
