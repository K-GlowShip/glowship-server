import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.rls_context import set_rls_context
from app.modules.delay_analysis.infrastructure.analysis_run import AnalysisRun

"""
    에이전트 노드 경계 STALE 확인용 실행 상태 조회기 (application/agent/run_status.py 의 RunStatusReader 구현)
      - 분석은 잠금 트랜잭션 밖에서 오래 돌므로, 조회마다 짧은 세션을 새로 열고 닫습니다.
        그래프 실행 내내 트랜잭션을 열어 두지 않고, 다른 트랜잭션(수집 적재의 STALE 판정)이 커밋한 상태를 바로 봅니다.
      - analysis_runs.status 열만 읽습니다. ORM 객체를 읽지 않으므로 세션 캐시(식별자 맵)의 옛 값을 보지 않습니다.
      - 고객사 범위 RLS 를 받는 DB 역할로 접속해도 읽히도록, 조회 전에 app.company_id 를 설정합니다.
        워커 역할의 정책은 app.company_id 만 보므로 app.user_id 는 비웁니다.
      - 조회 조건에도 company_id 를 넣습니다. RLS 가 적용되지 않는 접속(로컬 superuser 등)에서도 다른 고객사 실행은
        읽히지 않습니다(analysis_runs_uk1 = company_id, analysis_run_id).
      - 워커 역할(분석 실행) 전용입니다. 앱 역할 정책은 app.user_id 의 사용자까지 확인하므로 이 조회기로는 실행이 보이지 않고
        None(= STALE 아님)을 돌려줍니다. 앱 역할에서 쓰면 STALE 을 놓치니 쓰지 않습니다(로컬 v6.2 DB 에서 확인).
      - None 은 정상 흐름에서는 나오지 않습니다(잘못된 역할·고객사로 연결했거나 실행이 지워진 경우).
        그래프는 None 을 STALE 아님으로 보고 계속 진행하므로, 알아챌 수 있게 경고 로그를 남깁니다.
        None 일 때 멈출지는 그래프 연결(백그라운드 실행) 작업에서 정합니다.
      - DB 조회가 실패하면(연결 오류 등) 예외를 그대로 올립니다. 재시도·실패 처리는 그래프 실행 쪽 몫입니다.
      - 읽기 전용입니다. 세션을 커밋하지 않고 닫습니다(닫을 때 롤백).
"""

logger = logging.getLogger(__name__)


class AnalysisRunStatusReader:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        company_id: uuid.UUID,
    ):
        """company_id: 분석 실행이 속한 고객사. 실행마다 조회기를 하나 만들어 그래프 config 로 넘깁니다."""
        self.session_factory = session_factory
        self.company_id = company_id

    # Read
    async def get_status(self, analysis_run_id: uuid.UUID) -> str | None:
        """실행의 현재 상태. 없거나 다른 고객사 실행이거나 RLS 로 보이지 않으면 None 을 반환합니다(경고 로그)."""
        async with self.session_factory() as db:
            await set_rls_context(db, user_id=None, company_id=self.company_id)
            status = await db.scalar(
                select(AnalysisRun.status).where(
                    AnalysisRun.company_id == self.company_id,
                    AnalysisRun.analysis_run_id == analysis_run_id,
                )
            )
        if status is None:
            logger.warning(
                "실행 상태를 읽지 못해 STALE 아님으로 진행합니다 (analysis_run_id=%s, company_id=%s)."
                " 워커 역할 세션·고객사가 맞는지, 실행이 지워지지 않았는지 확인하세요",
                analysis_run_id,
                self.company_id,
            )
        return status
