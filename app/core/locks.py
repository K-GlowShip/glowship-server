import uuid
from collections.abc import Iterable

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.delay_analysis.infrastructure.analysis_run import AnalysisRun

"""
    고객사·분석 실행 잠금 (serviceflow_glowship.md 5.2절 잠금 규약)
      - L1 고객사 잠금: 수집 적재, 분석 입력 확정, 운영 기준 저장, 재분석 생성
      - L2 실행 잠금: STALE 판정, 최종 선택, 재분석 생성, 분석 완료 처리
      - 순서는 항상 L1 → L2 입니다. 여러 실행은 실행 ID 순서로 잠급니다. 최종 선택은 L2만 잡습니다.
      - 두 잠금 모두 트랜잭션 범위이며 commit·rollback 시 풀립니다.
      - 에이전트 분석처럼 오래 걸리는 작업은 잠금을 잡은 트랜잭션 안에서 실행하지 않습니다.
"""

# L1은 companies 행 FOR UPDATE 대신 고객사 키 트랜잭션 잠금을 씁니다.
# 앱·워커 역할은 companies 에 SELECT 권한만 있어 FOR UPDATE 를 쓸 수 없기 때문입니다.
_COMPANY_LOCK_SQL = text(
    "SELECT pg_advisory_xact_lock(hashtextextended(CAST(:company_id AS text), 0))"
)


async def lock_company(session: AsyncSession, company_id: uuid.UUID) -> None:
    """L1 고객사 잠금. 같은 고객사를 잠근 다른 트랜잭션이 끝날 때까지 기다립니다."""
    await session.execute(_COMPANY_LOCK_SQL, {"company_id": str(company_id)})


async def lock_analysis_run(
    session: AsyncSession, analysis_run_id: uuid.UUID
) -> AnalysisRun | None:
    """L2 실행 잠금. 잠근 실행을 최신 값으로 다시 읽어 반환하고, 없으면 None 을 반환합니다."""
    stmt = (
        select(AnalysisRun)
        .where(AnalysisRun.analysis_run_id == analysis_run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return await session.scalar(stmt)


async def lock_analysis_runs(
    session: AsyncSession, analysis_run_ids: Iterable[uuid.UUID]
) -> list[AnalysisRun]:
    """여러 실행 L2 잠금. 실행 ID 순서로 하나씩 잠그고, 없는 실행은 결과에서 뺍니다."""
    runs = []
    for analysis_run_id in sorted(set(analysis_run_ids)):
        run = await lock_analysis_run(session, analysis_run_id)
        if run is not None:
            runs.append(run)
    return runs
