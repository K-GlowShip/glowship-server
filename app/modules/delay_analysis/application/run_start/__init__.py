"""
분석 자동 시작 (GLOW-128, 이슈 #51, docs/analysis/auto-start.md)
  - transitions: 실행 상태 전이(시작·단계)와 서버 시작 정리 조회. 짧은 트랜잭션, L2 만 잡는다.
  - outcome_save: 그래프 종료 상태 → 저장 함수(result_save_service) 연결.
  - engine_runtime: 실제 실행 도구 묶음 조립(엔진 입력·도구·설명).
  - runner: 서버 안 asyncio 실행기(동시 실행 수 제한, 중복 시작 방지, 마지막 잡기, 시작 정리·종료).
"""
