import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import UUID, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.decision.application.draft_policy import DraftTarget
from app.modules.decision.infrastructure.member_draft import MemberDraft

"""
    의견·선택 초안 저장 리포지토리 (API 18 POST /analysis-runs/{runId}/drafts, API 명세 v6.2 4.6절)
      - 읽기는 직접 SQL, 초안 한 행 추가는 ORM 엔터티(db.add + flush) (docs/backend/data-access.md).
      - 초안은 L2 실행 잠금 대상이 아니므로(serviceflow 5.2절) 잠그지 않고 읽습니다.
      - 운영에서는 RLS 가 고객사 범위를 강제하지만, 로컬 개발 DB 는 슈퍼유저라 RLS 를 우회하므로
        company_id 조건을 명시해 같은 결과가 나오게 한다 (ERD 설명 4.6).
"""

_UUID = UUID(as_uuid=True)

# 실행과 그 지연 에피소드의 열림 여부 (delay_events.closed_at 이 없으면 열림)
_DRAFT_TARGET = text(
    """
    SELECT r.status, r.is_latest, r.failure_code, e.closed_at IS NULL AS episode_open
      FROM analysis_runs r
      JOIN delay_events e
        ON e.company_id = r.company_id AND e.delay_event_id = r.delay_event_id
     WHERE r.company_id = :company_id AND r.analysis_run_id = :analysis_run_id
    """
).bindparams(
    bindparam("company_id", type_=_UUID), bindparam("analysis_run_id", type_=_UUID)
)

_OPTION_IN_RUN = text(
    """
    SELECT EXISTS (
        SELECT 1
          FROM response_options
         WHERE company_id = :company_id
           AND analysis_run_id = :analysis_run_id
           AND response_option_id = :response_option_id
    )
    """
).bindparams(
    bindparam("company_id", type_=_UUID),
    bindparam("analysis_run_id", type_=_UUID),
    bindparam("response_option_id", type_=_UUID),
)


@dataclass(frozen=True)
class NewMemberDraft:
    member_draft_id: uuid.UUID
    analysis_run_id: uuid.UUID
    user_id: uuid.UUID
    response_option_id: uuid.UUID | None
    opinion_text: str | None
    created_at: datetime


class MemberDraftRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # Read
    async def find_draft_target(
        self, company_id: uuid.UUID, analysis_run_id: uuid.UUID
    ) -> DraftTarget | None:
        """초안 판정에 필요한 실행 상태와 에피소드 열림 여부. 고객사에 없는 실행이면 None"""
        row = (
            await self.db.execute(
                _DRAFT_TARGET,
                {"company_id": company_id, "analysis_run_id": analysis_run_id},
            )
        ).one_or_none()
        if row is None:
            return None
        return DraftTarget(
            status=row.status,
            is_latest=row.is_latest,
            failure_code=row.failure_code,
            episode_open=row.episode_open,
        )

    async def option_in_run(
        self,
        company_id: uuid.UUID,
        analysis_run_id: uuid.UUID,
        response_option_id: uuid.UUID,
    ) -> bool:
        """대응안이 같은 실행의 것인가 (fk_member_drafts__response_option_id 와 같은 조건)"""
        return bool(
            await self.db.scalar(
                _OPTION_IN_RUN,
                {
                    "company_id": company_id,
                    "analysis_run_id": analysis_run_id,
                    "response_option_id": response_option_id,
                },
            )
        )

    # Write
    async def insert_member_draft(
        self, company_id: uuid.UUID, draft: NewMemberDraft
    ) -> None:
        """초안 한 행 (member_drafts_ck1: 대응안·의견 중 하나 이상)"""
        self.db.add(
            MemberDraft(
                member_draft_id=draft.member_draft_id,
                company_id=company_id,
                analysis_run_id=draft.analysis_run_id,
                user_id=draft.user_id,
                response_option_id=draft.response_option_id,
                opinion_text=draft.opinion_text,
                created_at=draft.created_at,
            )
        )
        await self.db.flush()
