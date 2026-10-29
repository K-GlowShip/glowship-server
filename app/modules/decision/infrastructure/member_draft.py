import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.orm import Mapped

from app.core.orm import Base, uuid_pk


class MemberDraft(Base):
    """Client 멤버의 대응안 의견 초안 (선택안 또는 의견 중 하나 이상)"""

    __tablename__ = "member_drafts"
    __table_args__ = (
        UniqueConstraint("company_id", "member_draft_id", name="member_drafts_uk1"),
        CheckConstraint(
            "response_option_id IS NOT NULL OR opinion_text IS NOT NULL",
            name="member_drafts_ck1",
        ),
        ForeignKeyConstraint(
            ["company_id", "analysis_run_id"],
            ["analysis_runs.company_id", "analysis_runs.analysis_run_id"],
            name="fk_member_drafts__analysis_run_id",
        ),
        ForeignKeyConstraint(
            ["company_id", "user_id"],
            ["users.company_id", "users.user_id"],
            name="fk_member_drafts__user_id",
        ),
        ForeignKeyConstraint(
            ["company_id", "analysis_run_id", "response_option_id"],
            [
                "response_options.company_id",
                "response_options.analysis_run_id",
                "response_options.response_option_id",
            ],
            name="fk_member_drafts__response_option_id",
        ),
    )

    member_draft_id: Mapped[uuid_pk]
    company_id: Mapped[uuid.UUID]
    analysis_run_id: Mapped[uuid.UUID]
    user_id: Mapped[uuid.UUID]
    response_option_id: Mapped[uuid.UUID | None]
    opinion_text: Mapped[str | None]
    created_at: Mapped[datetime]
