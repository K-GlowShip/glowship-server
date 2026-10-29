from functools import lru_cache

from langgraph.graph.state import CompiledStateGraph

from app.modules.delay_analysis.application.agent.graph import build_analysis_graph

"""
    delay_analysis 모듈 의존성 조립
      - 컴파일된 분석 그래프는 상태가 없으므로 한 번만 만들어 재사용합니다.
"""


@lru_cache
def get_analysis_graph() -> CompiledStateGraph:
    return build_analysis_graph()
