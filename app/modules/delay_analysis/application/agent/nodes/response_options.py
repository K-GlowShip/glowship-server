from app.modules.delay_analysis.application.agent.state import AnalysisState


async def response_options_node(state: AnalysisState) -> dict:
    """
    대응안 분석 에이전트
      - 운송 단계별 검토 대상(계획 유지·출항 전 항공 전환·한국 항공 보충·동부 창고 출고)의
        실행 가능성을 검사하고(T9) 대응안별 예상 일정·주문 회복 결과를 계산합니다(T10).
      - 결과는 조정 에이전트로 돌아갑니다.

    TODO: 조회·계산 도구 연결 (현재 뼈대는 빈 결과 반환)
    """
    return {
        "option_results": {},
        "visited_nodes": ["response_options"],
    }
