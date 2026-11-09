import uuid
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import UUID, Text, bindparam, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.data_sync.infrastructure.repository.data_sync_run_repository import (
    last_applied_sync_started_at,
)
from app.modules.delay_analysis.infrastructure.analysis_enums import TriggerType
from app.modules.delay_analysis.infrastructure.analysis_run import AnalysisRun
from app.modules.delay_analysis.infrastructure.analysis_run_data_version import (
    AnalysisRunDataVersion,
)

"""
    분석 실행 생성(입력 확정)에 필요한 조회·기록 (명세 v6.2 8절, serviceflow 5.2 처리 단계 1)
      - 모든 조회는 고객사 범위(company_id)를 조건에 넣는다. RLS 문맥은 호출하는 쪽(수집 적재·재분석 API)이 연다.
      - 운영 기준·입력 순번·데이터 버전 규칙은 데이터 팀 엔진(snapshot.build, store.input_seq)과 같고,
        overlay 실행 21건의 저장값과 같은지 시험으로 확인한다.
"""

_UUID = UUID(as_uuid=True)

_DELAY_EVENT = text(
    """
    SELECT delay_event_id, shipment_id, closed_at
      FROM delay_events
     WHERE company_id = :company_id AND delay_event_id = :delay_event_id
    """
).bindparams(
    bindparam("company_id", type_=_UUID), bindparam("delay_event_id", type_=_UUID)
)

_SNAPSHOT_SHIPMENT = text(
    """
    SELECT shipment_id
      FROM shipment_schedule_snapshots
     WHERE company_id = :company_id AND shipment_schedule_snapshot_id = :snapshot_id
    """
).bindparams(
    bindparam("company_id", type_=_UUID), bindparam("snapshot_id", type_=_UUID)
)

# 엔진 snapshot.build: 기준일 D 에 적용 중인 운영 기준 = 시작일이 D 이하인 버전 중 가장 큰 버전
_OPERATING_RULE = text(
    """
    SELECT operating_rule_set_id
      FROM operating_rule_sets
     WHERE company_id = :company_id AND effective_from <= :basis_date
     ORDER BY version_no DESC
     LIMIT 1
    """
).bindparams(bindparam("company_id", type_=_UUID))

# 데이터 버전: 수집 도메인마다 적용 순번이 실행의 입력 순번 이하인 가장 최근(순번이 가장 큰) 수집 1건.
# serviceflow 5.2 "왜 시각이 아니라 순번인가"와 STALE 판정(순번 기준)에 맞춘다. 엔진 snapshot.build 는 같은 뜻을
# 시작 시각(confirm_t)으로 고르고, overlay 의 검증 통과 실행 20건(100행)은 두 규칙 모두 같은 결과다.
_DOMAIN_LATEST_SYNC_RUNS = text(
    """
    SELECT DISTINCT ON (source_domain) data_sync_run_id
      FROM data_sync_runs
     WHERE company_id = :company_id
       AND applied_seq IS NOT NULL AND applied_seq <= :input_seq
     ORDER BY source_domain, applied_seq DESC
    """
).bindparams(bindparam("company_id", type_=_UUID))

_LATEST_RUN_CODE_OF_DAY = text(
    """
    SELECT MAX(run_code)
      FROM analysis_runs
     WHERE company_id = :company_id AND run_code LIKE :day_prefix || '%'
    """
).bindparams(bindparam("company_id", type_=_UUID), bindparam("day_prefix", type_=Text))


# 고객사의 마지막 적재 수집 시작(초 단위, 엔진이 읽는 모양). 넘겨받은 실행 시각 확인에 쓴다(이슈 #50)
@dataclass(frozen=True)
class DelayEventRef:
    delay_event_id: uuid.UUID
    shipment_id: uuid.UUID
    closed_at: datetime | None


class RunCreationRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def find_delay_event(
        self, company_id: uuid.UUID, delay_event_id: uuid.UUID
    ) -> DelayEventRef | None:
        row = (
            await self.db.execute(
                _DELAY_EVENT,
                {"company_id": company_id, "delay_event_id": delay_event_id},
            )
        ).one_or_none()
        return DelayEventRef(*row) if row is not None else None

    async def find_snapshot_shipment_id(
        self, company_id: uuid.UUID, snapshot_id: uuid.UUID
    ) -> uuid.UUID | None:
        return await self.db.scalar(
            _SNAPSHOT_SHIPMENT, {"company_id": company_id, "snapshot_id": snapshot_id}
        )

    async def find_auto_run(
        self, delay_event_id: uuid.UUID, snapshot_id: uuid.UUID
    ) -> AnalysisRun | None:
        """같은 지연·같은 트리거 스냅샷의 AUTO 실행 (analysis_runs_auto_trigger_uq 대상)"""
        stmt = select(AnalysisRun).where(
            AnalysisRun.delay_event_id == delay_event_id,
            AnalysisRun.trigger_type == TriggerType.AUTO,
            AnalysisRun.trigger_shipment_schedule_snapshot_id == snapshot_id,
        )
        return await self.db.scalar(stmt)

    async def find_max_version_no(self, delay_event_id: uuid.UUID) -> int | None:
        stmt = select(func.max(AnalysisRun.version_no)).where(
            AnalysisRun.delay_event_id == delay_event_id
        )
        return await self.db.scalar(stmt)

    async def find_parent_run_id(self, delay_event_id: uuid.UUID) -> uuid.UUID | None:
        """새 실행의 부모: 지금 현재 실행(is_latest). 없으면 가장 큰 버전의 실행"""
        stmt = (
            select(AnalysisRun.analysis_run_id)
            .where(AnalysisRun.delay_event_id == delay_event_id)
            .order_by(AnalysisRun.is_latest.desc(), AnalysisRun.version_no.desc())
            .limit(1)
        )
        return await self.db.scalar(stmt)

    async def find_operating_rule_set_id(
        self, company_id: uuid.UUID, basis: date
    ) -> uuid.UUID | None:
        return await self.db.scalar(
            _OPERATING_RULE, {"company_id": company_id, "basis_date": basis}
        )

    async def find_latest_run_code_of_day(
        self, company_id: uuid.UUID, day_prefix: str
    ) -> str | None:
        return await self.db.scalar(
            _LATEST_RUN_CODE_OF_DAY,
            {"company_id": company_id, "day_prefix": day_prefix},
        )

    async def now(self) -> datetime:
        """실제 현재 시각(clock_timestamp). 트랜잭션 시작 시각(now())이 아니라 잠금을 잡은 뒤의 시각이다"""
        return await self.db.scalar(text("SELECT clock_timestamp()"))

    async def find_last_applied_sync_started_at(
        self, company_id: uuid.UUID
    ) -> datetime | None:
        """
        고객사의 적재된(적용 순번 있는) 수집 중 가장 늦은 시작 시각(초 단위). 없으면 None.
        엔진은 실행 시각 t 에 "시작 ≤ t 인 적재 수집의 최대 순번"을 입력 순번으로 본다(초 아래를 버림,
        PR #36 check_input_seq). 실행 시각이 이 값보다 이르면 실행의 input_seq 와 어긋난다.
        """
        return await last_applied_sync_started_at(self.db, company_id)

    async def find_domain_latest_sync_run_ids(
        self, company_id: uuid.UUID, input_seq: int
    ) -> list[uuid.UUID]:
        result = await self.db.scalars(
            _DOMAIN_LATEST_SYNC_RUNS,
            {"company_id": company_id, "input_seq": input_seq},
        )
        return list(result)

    async def find_recorded_sync_run_ids(
        self, analysis_run_id: uuid.UUID
    ) -> set[uuid.UUID]:
        stmt = select(AnalysisRunDataVersion.data_sync_run_id).where(
            AnalysisRunDataVersion.analysis_run_id == analysis_run_id
        )
        return set(await self.db.scalars(stmt))
