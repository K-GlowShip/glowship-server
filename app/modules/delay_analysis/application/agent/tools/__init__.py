"""
에이전트 도구 T1~T12 의 입력·출력 모델 (docs/agent-flow/tool-contracts.md, 계약 10)
  - common: 공통 타입·도구 이름 목록(ToolName)
  - lookup_schemas: 조회 도구 T1~T5·T8 (C 구현)
  - impact_schemas: 재고 영향 계산 도구 T7·T6 (C 구현)
  - option_schemas: 대응안 도구 T9·T10·T11 (A 구현)
  - T12 validate_agent_output 은 2차라 아직 모델이 없다(계약 8절).
"""
