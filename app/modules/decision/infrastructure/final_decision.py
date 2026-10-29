import uuid
from datetime import datetime

from sqlalchemy import ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.orm import Mapped

from app.core.orm import Base, uuid_pk


class FinalDecision(Base):
    """
    분석 실행별 최종 대응안 결정 (분석 실행당 1건)
      - checked_seq: 결정 시점에 확인한 데이터 적용 순번 (STALE 판정 기준)
    """

    __tablename__ = "final_decisions"
    __table_args__ = (
        UniqueConstraint("company_id", "final_decision_id", name="final_decisions_uk1"),
        UniqueConstraint("analysis_run_id", name="final_decisions_uk2"),
        UniqueConstraint("company_id", "idempotency_key", name="final_decisions_uk3"),
        ForeignKeyConstraint(
            ["company_id", "user_id"],
            ["users.company_id", "users.user_id"],
            name="fk_final_decisions__user_id",
        ),
        ForeignKeyConstraint(
            ["company_id", "analysis_run_id", "response_option_id"],
            [
                "response_options.company_id",
                "response_options.analysis_run_id",
                "response_options.response_option_id",
            ],
            name="fk_final_decisions__response_option_id",
        ),
    )

    final_decision_id: Mapped[uuid_pk]
    company_id: Mapped[uuid.UUID]
    analysis_run_id: Mapped[uuid.UUID]
    user_id: Mapped[uuid.UUID]
    response_option_id: Mapped[uuid.UUID]
    reason_text: Mapped[str]
    idempotency_key: Mapped[str]
    decided_at: Mapped[datetime]
    checked_seq: Mapped[int]
