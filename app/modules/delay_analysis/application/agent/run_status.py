import uuid
from typing import Protocol

from langchain_core.runnables import RunnableConfig

"""
    노드 경계 실행 상태 확인 (agentflow_glowship.md 5.5절)
      - 에이전트는 서비스가 제공하는 읽기 전용 조회로 analysis_runs.status 를 확인합니다.
      - 서비스가 STALE 로 바꾼 실행이면 남은 단계를 진행하지 않습니다.
      - 조회기는 그래프 실행 시 config["configurable"]["run_status_reader"] 로 주입합니다.
"""

STALE_STATUS = "STALE"


class RunStatusReader(Protocol):
    async def get_status(self, analysis_run_id: uuid.UUID) -> str | None: ...


async def is_run_stale(config: RunnableConfig, analysis_run_id: uuid.UUID) -> bool:
    """주입된 조회기가 없으면(뼈대·테스트 실행) STALE 이 아닌 것으로 봅니다."""
    reader: RunStatusReader | None = config.get("configurable", {}).get(
        "run_status_reader"
    )
    if reader is None:
        return False
    return await reader.get_status(analysis_run_id) == STALE_STATUS
