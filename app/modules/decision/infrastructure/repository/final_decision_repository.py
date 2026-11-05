import uuid
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import UUID, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.decision.application.selection_policy import (
    LeadTimes,
    SelectionOption,
    SelectionTarget,
)
from app.modules.decision.infrastructure.final_decision import FinalDecision

"""
    최종 선택 저장소 (API 19 POST /analysis-runs/{runId}/final-decision, API 명세 v6.2 4.6절)
      - 재확인 입력(SelectionTarget)을 DB 에서 읽습니다. 값과 뜻은 엔진 snapshot.build·recheck.selection_check 와 같습니다.
          운영 기준: 실행의 operating_rule_set_id, 선택일에 적용 중인 버전 = effective_from ≤ 선택일 중 version_no 최대
          리드타임: 실행의 운영 기준 operating_lead_times. 도착 창고 = 지연 선적의 창고, 공여 창고 = 고객사의 다른 US_3PL,
                    한국 창고 = KR_ORIGIN (엔진 snapshot.build 96~117행)
          최신 E: 선적의 현재 예상 3PL 입고일. 한국 출고 계획일: 선적의 planned_kr_release_date
          회복 주문 약속일: 그 대응안의 option_order_outcomes RECOVERED 와 at_risk_orders.promised_delivery_at 의 UTC 날짜
      - 같은 고객사의 Idempotency-Key 로 저장된 최종 선택을 찾고, 최종 선택 한 행을 ORM 으로 추가합니다.
      - 읽기는 직접 SQL, 한 행 추가는 ORM (docs/backend/data-access.md). 잠금은 서비스가 잡습니다(L2).
      - 운영에서는 RLS 가 고객사 범위를 강제하지만, 로컬 superuser 는 우회하므로 company_id 조건을 명시합니다.
"""

_UUID = UUID(as_uuid=True)


class SelectionInputError(RuntimeError):
    """재확인에 필요한 운영 기준 값(리드타임·창고)이 없음. 데이터 오류라 500 으로 둡니다."""


_RUN_CONTEXT = text(
    """
    SELECT r.status, r.is_latest, r.failure_code, r.operating_rule_set_id,
           e.closed_at IS NULL AS episode_open,
           s.planned_kr_release_date, s.current_expected_3pl_inbound_date,
           s.warehouse_id AS dest_warehouse_id, w.region_code AS dest_region_code
      FROM analysis_runs r
      JOIN delay_events e
        ON e.company_id = r.company_id AND e.delay_event_id = r.delay_event_id
      JOIN shipments s
        ON s.company_id = e.company_id AND s.shipment_id = e.shipment_id
      JOIN warehouses w
        ON w.company_id = s.company_id AND w.warehouse_id = s.warehouse_id
     WHERE r.company_id = :company_id AND r.analysis_run_id = :analysis_run_id
    """
).bindparams(
    bindparam("company_id", type_=_UUID), bindparam("analysis_run_id", type_=_UUID)
)

_ACTIVE_RULE = text(
    """
    SELECT operating_rule_set_id
      FROM operating_rule_sets
     WHERE company_id = :company_id AND effective_from <= :day
     ORDER BY version_no DESC
     LIMIT 1
    """
).bindparams(bindparam("company_id", type_=_UUID))

_WAREHOUSES = text(
    """
    SELECT warehouse_id, warehouse_type
      FROM warehouses
     WHERE company_id = :company_id
     ORDER BY warehouse_code
    """
).bindparams(bindparam("company_id", type_=_UUID))

_LEAD_TIMES = text(
    """
    SELECT lead_time_type, warehouse_id, to_region_code, days
      FROM operating_lead_times
     WHERE company_id = :company_id AND operating_rule_set_id = :rule_id
    """
).bindparams(bindparam("company_id", type_=_UUID), bindparam("rule_id", type_=_UUID))

_OPTION = text(
    """
    SELECT option_type, feasibility_status
      FROM response_options
     WHERE company_id = :company_id AND analysis_run_id = :analysis_run_id
       AND response_option_id = :response_option_id
    """
).bindparams(
    bindparam("company_id", type_=_UUID),
    bindparam("analysis_run_id", type_=_UUID),
    bindparam("response_option_id", type_=_UUID),
)

_RECOVERED_PROMISED = text(
    """
    SELECT (o.promised_delivery_at AT TIME ZONE 'UTC')::date AS promised
      FROM option_order_outcomes x
      JOIN at_risk_orders o
        ON o.company_id = x.company_id AND o.at_risk_order_id = x.at_risk_order_id
     WHERE x.company_id = :company_id AND x.response_option_id = :response_option_id
       AND x.outcome = 'RECOVERED'
     ORDER BY promised
    """
).bindparams(
    bindparam("company_id", type_=_UUID), bindparam("response_option_id", type_=_UUID)
)

_BY_IDEMPOTENCY_KEY = text(
    """
    SELECT final_decision_id, analysis_run_id, decided_at, checked_seq
      FROM final_decisions
     WHERE company_id = :company_id AND idempotency_key = :idempotency_key
    """
).bindparams(bindparam("company_id", type_=_UUID))


@dataclass(frozen=True)
class StoredFinalDecision:
    final_decision_id: uuid.UUID
    analysis_run_id: uuid.UUID
    decided_at: datetime
    checked_seq: int


@dataclass(frozen=True)
class NewFinalDecision:
    final_decision_id: uuid.UUID
    analysis_run_id: uuid.UUID
    user_id: uuid.UUID
    response_option_id: uuid.UUID
    reason_text: str
    idempotency_key: str
    decided_at: datetime
    checked_seq: int


class FinalDecisionRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # Read
    async def find_by_idempotency_key(
        self, company_id: uuid.UUID, idempotency_key: str
    ) -> StoredFinalDecision | None:
        row = (
            await self.db.execute(
                _BY_IDEMPOTENCY_KEY,
                {"company_id": company_id, "idempotency_key": idempotency_key},
            )
        ).one_or_none()
        return None if row is None else StoredFinalDecision(*row)

    async def find_selection_target(
        self,
        company_id: uuid.UUID,
        analysis_run_id: uuid.UUID,
        response_option_id: uuid.UUID,
        selection_day: date,
    ) -> SelectionTarget | None:
        """재확인 입력. 고객사에 없는 실행이면 None. 대응안이 그 실행의 것이 아니면 option=None"""
        run = (
            await self.db.execute(
                _RUN_CONTEXT,
                {"company_id": company_id, "analysis_run_id": analysis_run_id},
            )
        ).one_or_none()
        if run is None:
            return None
        active_rule_id = await self.db.scalar(
            _ACTIVE_RULE, {"company_id": company_id, "day": selection_day}
        )
        return SelectionTarget(
            status=run.status,
            is_latest=run.is_latest,
            failure_code=run.failure_code,
            episode_open=run.episode_open,
            operating_rule_set_id=run.operating_rule_set_id,
            active_operating_rule_set_id=active_rule_id,
            option=await self._option(company_id, analysis_run_id, response_option_id),
            lead_times=await self._lead_times(company_id, run),
            latest_expected_3pl_inbound_date=run.current_expected_3pl_inbound_date,
            planned_kr_release_date=run.planned_kr_release_date,
            selection_day=selection_day,
        )

    async def _option(
        self,
        company_id: uuid.UUID,
        analysis_run_id: uuid.UUID,
        response_option_id: uuid.UUID,
    ) -> SelectionOption | None:
        option = (
            await self.db.execute(
                _OPTION,
                {
                    "company_id": company_id,
                    "analysis_run_id": analysis_run_id,
                    "response_option_id": response_option_id,
                },
            )
        ).one_or_none()
        if option is None:
            return None
        promised = await self.db.scalars(
            _RECOVERED_PROMISED,
            {"company_id": company_id, "response_option_id": response_option_id},
        )
        return SelectionOption(
            option_type=option.option_type,
            feasibility_status=option.feasibility_status,
            recovered_promised_dates=tuple(promised),
        )

    async def _lead_times(self, company_id: uuid.UUID, run) -> LeadTimes:
        """엔진 snapshot.build 의 lt 와 같은 규칙. 필요한 값이 없으면 SelectionInputError"""
        warehouses = (
            await self.db.execute(_WAREHOUSES, {"company_id": company_id})
        ).all()
        dest = run.dest_warehouse_id
        donor = next(
            (
                w.warehouse_id
                for w in warehouses
                if w.warehouse_type == "US_3PL" and w.warehouse_id != dest
            ),
            None,
        )
        kr = next(
            (w.warehouse_id for w in warehouses if w.warehouse_type == "KR_ORIGIN"),
            None,
        )
        rows = (
            await self.db.execute(
                _LEAD_TIMES,
                {"company_id": company_id, "rule_id": run.operating_rule_set_id},
            )
        ).all()

        def days(lead_time_type, warehouse_id, to_region=None):
            for row in rows:
                if (
                    row.lead_time_type == lead_time_type
                    and row.warehouse_id == warehouse_id
                    and (to_region is None or row.to_region_code == to_region)
                ):
                    return int(row.days)
            return None

        region = run.dest_region_code
        values = {
            "oh_kr": days("OUTBOUND_HANDLING", kr),
            "air_kr": days("AIR_TRANSIT", kr, region),
            "inbound_dest": days("INBOUND_HANDLING", dest),
            "oh_donor": days("OUTBOUND_HANDLING", donor),
            "cd_cross": days("CUSTOMER_DELIVERY", donor, region),
            "oh_dest": days("OUTBOUND_HANDLING", dest),
            "cd_same": days("CUSTOMER_DELIVERY", dest, region),
        }
        missing = sorted(name for name, value in values.items() if value is None)
        if missing:
            raise SelectionInputError(
                f"실행 운영 기준({run.operating_rule_set_id})에 리드타임이 없음: {missing}"
            )
        return LeadTimes(**values)

    # Write
    async def insert_final_decision(
        self, company_id: uuid.UUID, decision: NewFinalDecision
    ) -> None:
        """최종 선택 한 행 (실행당 1건 final_decisions_uk2, 고객사 안 키 1건 uk3)"""
        self.db.add(
            FinalDecision(
                final_decision_id=decision.final_decision_id,
                company_id=company_id,
                analysis_run_id=decision.analysis_run_id,
                user_id=decision.user_id,
                response_option_id=decision.response_option_id,
                reason_text=decision.reason_text,
                idempotency_key=decision.idempotency_key,
                decided_at=decision.decided_at,
                checked_seq=decision.checked_seq,
            )
        )
        await self.db.flush()
