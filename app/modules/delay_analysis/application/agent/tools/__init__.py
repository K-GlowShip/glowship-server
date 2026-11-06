"""
에이전트 도구 T1~T12 의 입력·출력 모델 (docs/agent-flow/tool-contracts.md, 계약 10)
  - common: 공통 타입·도구 이름 목록(ToolName)
  - lookup_schemas: 조회 도구 T1~T5·T8 (C 구현)
  - impact_schemas: 재고 영향 계산 도구 T7·T6 (C 구현)
  - option_schemas: 대응안 도구 T9·T10·T11 (A 구현)
  - option_feasibility: T9 check_option_feasibility 구현
  - option_comparison: T11 compare_options 구현
  - validation_schemas: 설명 검증 도구 T12 (C 구현, 계약 8절)
  - output_validation: T12 validate_agent_output 구현 (엔진 explain.validator 감싸기)
"""
