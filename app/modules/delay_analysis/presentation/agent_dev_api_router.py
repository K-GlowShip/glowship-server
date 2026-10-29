from fastapi import APIRouter, Depends
from langgraph.graph.state import CompiledStateGraph

from app.modules.delay_analysis.dependencies import get_analysis_graph
from app.modules.delay_analysis.presentation.agent_schemas import (
    AgentDryRunRequest,
    AgentDryRunResponse,
)

# 개발 확인용 라우터: prod 환경에서는 main.py 에서 등록하지 않습니다.
router = APIRouter(prefix="/api/v1/dev/agent", tags=["Dev (에이전트 확인)"])


@router.post(
    "/dry-run",
    summary="지연 분석 에이전트 그래프 실행 확인",
    description=(
        "빈 노드로 연결된 지연 분석 그래프(분석 준비 → 조정 ⇄ 재고 영향·대응안 분석"
        " → 비교 → 설명 → 검증)를 한 번 실행하고, 거친 노드 순서와 종료 상태를 반환합니다."
        " DB를 읽거나 쓰지 않습니다."
    ),
    response_model=AgentDryRunResponse,
)
async def dry_run_agent(
    request: AgentDryRunRequest,
    graph: CompiledStateGraph = Depends(get_analysis_graph),
) -> AgentDryRunResponse:
    result = await graph.ainvoke(request.model_dump())

    return AgentDryRunResponse(
        analysis_run_id=result["analysis_run_id"],
        outcome=result["outcome"],
        last_step=result.get("step"),
        visited_nodes=result["visited_nodes"],
    )
