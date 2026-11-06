from functools import lru_cache

from fastapi import Depends
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.modules.delay_analysis.application.agent.graph import build_analysis_graph
from app.modules.delay_analysis.application.analysis_run_detail_service import (
    AnalysisRunDetailService,
)
from app.modules.delay_analysis.application.delay_event_query_service import (
    DelayEventQueryService,
)
from app.modules.delay_analysis.infrastructure.repository.analysis_run_detail_repository import (
    AnalysisRunDetailRepository,
)
from app.modules.delay_analysis.infrastructure.repository.delay_event_read_repository import (
    DelayEventReadRepository,
)

"""
    delay_analysis 모듈 의존성 조립
      - 컴파일된 분석 그래프는 상태가 없으므로 한 번만 만들어 재사용합니다.
      - 지연 조회 서비스는 요청 단위 DB 세션(get_db)으로 만듭니다.
"""


def get_delay_event_query_service(
    db: AsyncSession = Depends(get_db),
) -> DelayEventQueryService:
    return DelayEventQueryService(db=db, repo=DelayEventReadRepository(db))


def get_analysis_run_detail_service(
    db: AsyncSession = Depends(get_db),
) -> AnalysisRunDetailService:
    return AnalysisRunDetailService(
        db=db,
        repo=AnalysisRunDetailRepository(db),
        delay_repo=DelayEventReadRepository(db),
    )


@lru_cache
def get_analysis_graph() -> CompiledStateGraph:
    return build_analysis_graph()
