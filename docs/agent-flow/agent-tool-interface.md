# 에이전트 도구 호출 인터페이스

노드가 도구를 부르는 방식과, 도구 함수가 지켜야 할 모양을 정합니다. 도구를 만드는 쪽(조회·재고 영향 = 개발 1, 대응안 T9~T11 = A, 결과 검증 T12·설명 = 개발 3)과 노드에서 부르는 쪽(개발 2)이 이 문서를 기준으로 맞춥니다.

- 코드: `app/modules/delay_analysis/application/agent/tools/context.py`(`ToolContext`), `registry.py`(`ToolSpec`·`ToolRegistry`·`call_tool`·`pure_tool`·`code_error`), `agent/runtime.py`(실행 도구 묶음 `AnalysisRuntime`, 5.2절), 노드 `agent/nodes/*`
- 시험: `tests/modules/delay_analysis/test_tool_registry.py`(등록표), `test_analysis_graph.py`(그래프 흐름, 가짜 묶음 `agent/fake_runtime.py`)
- 그래프 동작: [지연 분석 에이전트 그래프](delay-analysis-graph.md)
- 도구 입출력 모델: `docs/agent-flow/tool-contracts.md`(계약 10), `agent/tools/*_schemas.py`
- 상태: 2026-11-05 세 개발 세션 합의(인터페이스 초안 1~5판) + 최종 분담안 v2(22시, T9~T11은 A). 결정 4·5는 대기(6절).

## 1. 누가 무엇을 만드나

| 것 | 만드는 쪽 | 파일 |
| --- | --- | --- |
| `ToolContext`, 등록표, `call_tool`(등록·형식 확인·재시도·추적), 노드·그래프, 결과 → 저장 넘김 | 개발 2 | `agent/tools/context.py`·`registry.py`, `agent/graph.py`·`state.py`·`nodes/*`, `application/result_save/*` |
| 엔진 입력 `build_engine_input`·`EngineInput`·`EngineHold`, `judge_no_delay`, `decision_of` | 개발 1 | `infrastructure/engine/engine_input.py` |
| T1~T5·T8 조회, T7·T6 계산 | 개발 1 | `agent/tools/` 새 파일 |
| T9 대응안 후보·사전 판정, T10 대응안 결과, T11 추천 순위 | A | `agent/tools/option_feasibility.py`(T9, #43), `option_comparison.py`(T11, #44), T10은 T6·T7 머지 뒤 |
| T12 결과 검증, 설명 생성 `generate_explanation(ctx, *, now)`, 설명 자산 확인 `verify_explanation_assets()` | 개발 3 | 설명 모듈 |

- 도구 함수는 **노드·상태·등록표를 import하지 않습니다.** 등록과 노드 호출은 개발 2가 합니다.
- 도구는 업무 DB에 쓰지 않습니다(agentflow 5.5). 엔진 값은 `ctx.engine`으로만 읽습니다.

## 2. 실행 한 번의 흐름

```
서비스: 실행 생성(입력 확정, #21) → 커밋 → 그래프 실행(트랜잭션 밖)
 prepare
   1) judge_no_delay(session, run)          NoDelay 면 종료 → save_no_delay   (엔진 저장소를 읽기 전, 엔진 하네스 순서)
   2) build_engine_input(session, run)      EngineHold 면 ON_HOLD(VALIDATING) 종료 → save_analysis_failure
   3) ctx.engine = EngineInput               실행마다 한 번(저장소 읽기 고객사당 약 9.5초, 개발 1 LRU)
 coordinator: T1                                  (재고 영향 뒤 T6 이 NO_RISK 면 decision_of 뒤 NO_RISK 종료)
 inventory_impact: T2·T3·T4·T5·T8 → T7 → T6       (부르는 순서는 계약 Q3)
 response_options: T2·T8 다시 조회 → T9 → T10     (설계 agentflow 5.3. 재사용 안은 tools/might_better_way/might_better_way.md D2-1)
 compare: T11 → 엔진 대조 → decision_of(ctx.engine) (대조가 다르면 FAILED. 의존 범위·판정 서명)
 explain: explain(ctx, now)                      (최대 3회는 함수 안. 실패면 validate 없이 종료)
 validate: T12                                   (최종 설명을 한 번 더 확인)
서비스: 결과 저장 → 커밋
```

- **판정 서명은 설명보다 먼저입니다.** `explanation.v1`의 필수 필드 `decision_signature_hash`가 판정 서명 SHA-256입니다.
- **엔진 계산은 실행마다 한 번입니다.** 엔진 `pipeline.analyze(snap)`(64~79행)이 예측 층·주문 층·영향 집합·결과 분류·대응안 평가·추천·판정 서명·의존 범위를 함께 냅니다. 조회·재고 영향 도구(T1~T8)와 설명은 그 결과(`ctx.engine.snap`·`ctx.engine.analysis`)에서 옮기기만 합니다.
- **대응안 T9~T11은 A의 함수가 계약 입력으로 계산합니다**(최종 분담안 v2). 판정 서명은 엔진 결과로 만들어지므로, 노드는 T10·T11 출력이 `ctx.engine.analysis["options"]`(상태·사유·순위·추천)와 같은지 확인하고 다르면 FAILED로 멈춥니다(GLOW-129, 서명과 화면 값이 어긋나지 않게).
- STALE 확인 경계는 조정 에이전트 진입·설명 진입입니다(`graph.with_stale_check`, 조회기는 `config["configurable"]["run_status_reader"]`, GLOW-98).
- **설명 함수 모양(11-06 확인):** 노드는 `explain(ctx, now)`로 부릅니다. 개발 3의 작업 중 코드 `generate_explanation`은 `(snap, analysis, analysis_run_id, shipment_code, now, adapter, tracer)`를 받고 `ExplanationPassed`·`ExplanationFailed`를 돌려줍니다. 둘을 잇는 작은 함수는 "실제 도구 교체" 때 만듭니다. `shipment_code`는 T1 출력에 없어(계약 3.1) 어디서 읽을지 그때 개발 1·3과 정합니다.

## 3. `ToolContext`

```python
@dataclass(frozen=True)
class ToolContext:
    company_id: uuid.UUID
    analysis_run_id: uuid.UUID
    base_date: date  # 기준일 D (실행 started_at 의 UTC 날짜)
    input_seq: int  # 실행 입력 순번 (#21)
    engine: EngineInputLike  # 개발 1 EngineInput (snap·analysis·store·실행 ID·기준일·입력 순번)
    tracer: ToolAttemptTracer | None = None  # GLOW-124 RunTracer
```

- `EngineInputLike`·`ToolAttemptTracer`는 도구·`call_tool`이 쓰는 최소 모양(Protocol)입니다. 실제 타입(개발 1 `EngineInput`, GLOW-124 `RunTracer`)을 import하지 않아 작업 순서가 서로 묶이지 않습니다.
- `EngineInput`(개발 1): `analysis_run_id`·`company_id`·`delay_event_id`·`shipment_id`·`base_date`·`input_seq`, `confirm_t`(엔진 시각), `snap`(`snapshot.build` 결과), `analysis`(`pipeline.analyze(snap)`, **`build_engine_input` 안에서 스레드로 한 번 계산**(사용자 결정 11-06, DV1-3: 나중에 동기 속성으로 계산하면 이벤트 루프가 멈춤). 키 `status`·`result_class`·`signature`·`sha`·`fc`·`ol`·`k_imp`·`k_watch`·`options`·`scope`), `store`(스냅샷에서 되돌릴 수 없는 원값. 예: T4 정수 합계).
- `EngineInputLike`가 담는 칸(DV1-5, 11-06): `snap`·`analysis`에 더해 조회 도구가 읽는 `store`와 T1 입력 확인용 `analysis_run_id`·`delay_event_id`·`base_date`·`input_seq`. 가짜 묶음(`fake_runtime.FakeEngineInput`)도 같은 칸을 가집니다.
- DB 세션은 넣지 않습니다. 도구가 DB를 직접 읽으면 입력 순번 뒤 변경이 섞일 수 있습니다.

## 4. 도구 함수 모양

```python
async def get_delay_context(
    ctx: ToolContext, payload: GetDelayContextInput
) -> DelayContext: ...
async def get_inventory_snapshot(
    ctx: ToolContext, payload: None
) -> InventorySnapshot: ...  # 입력 없음


# 동기 순수 함수(A의 대응안 도구 등)는 pure_tool 로 감싸 등록한다
def check_option_feasibility(
    data: CheckOptionFeasibilityInput,
) -> OptionFeasibility: ...


ToolSpec(
    pure_tool(check_option_feasibility), CheckOptionFeasibilityInput, OptionFeasibility
)
```

- 인자는 `ctx`와 계약 입력 모델, 반환은 계약 출력 모델입니다(필드를 더하지 않음). T2~T5·T8은 입력이 없어 `payload: None`입니다(범위는 엔진 입력이 정함, 계약 3절).
- `pure_tool`은 동기 함수를 스레드(`asyncio.to_thread`)에서 돌려 이벤트 루프를 막지 않습니다. `ctx`는 넘기지 않습니다(순수 함수는 입력 모델만 받음).
- **오류는 `agent/retry.py`(#28) 분류로 던집니다.**

| 상황 | 도구가 할 것 | `call_tool` 결과 |
| --- | --- | --- |
| 원천 응답 지연·일시 오류 | `TransientToolError` | 최대 2회 더 부름, 3회면 FAILED `TOOL_TEMPORARY_ERROR_EXHAUSTED`(소진) |
| 시간 초과 | 던지지 않음(`ToolSpec.timeout_seconds`) | 최대 2회 더 부름, 3회면 FAILED `TOOL_TIMEOUT_EXHAUSTED`(소진) |
| 고객사 범위 밖 | `PermissionDeniedError` | 재시도 없이 FAILED `PERMISSION_DENIED` |
| 필수 자료 부족 | **던지지 않음**(결정 1). 보류는 prepare의 `EngineHold`에서만 | 그래도 `RequiredDataError`가 나면 재시도 없이 FAILED `UNEXPECTED_TOOL_ERROR`(메시지에 원래 코드) |
| 대응안 하나의 사전 조건 실패 | 던지지 않음 — 그 대응안의 결과(`NOT_FEASIBLE`·집계 null, 명세 4.5 G-3, 계약 Q7) | 실행은 계속 |
| 입력·출력 형식이 계약 모델이 아님, 등록되지 않은 도구, 그 밖의 예외(예: T11 정렬 값 null `ValueError`) | — | 재시도 없이 FAILED `UNEXPECTED_TOOL_ERROR` |

## 5. 등록표와 `call_tool`

```python
registry = ToolRegistry({ToolName.GET_DELAY_CONTEXT: ToolSpec(fn, GetDelayContextInput, DelayContext, timeout_seconds=…), ...})
registry.require([...])        # 그래프를 만들 때 부를 도구가 다 있는지 (없으면 ValueError)
out = await call_tool(ctx, ToolName.GET_DELAY_CONTEXT, payload, step=AnalysisStep.ANALYZING_IMPACT, registry=registry)
```

- 등록표 열쇠는 계약 도구 이름 `ToolName`(12개)만 받습니다. 등록되지 않은 이름은 부르지 않습니다(AIR-001).
- **`step`은 부르는 노드가 정합니다.** 재고 영향 노드 `ANALYZING_IMPACT`, 대응안 노드 `ANALYZING_OPTIONS`(대응안 분석도 T2·T8을 부름, agentflow 5.3), 비교·설명·결과 검증 `VALIDATING_OUTPUT`. 엔진 입력 보류는 `VALIDATING`.
- 멈추면 `ToolFailure`(실패 요약)를 올립니다. 노드는 그 요약을 그대로 서비스에 넘기고, 서비스가 `save_analysis_failure`로 저장합니다.
- 시험은 등록표를 가짜 도구로 바꿔 끼워 그래프 흐름을 먼저 확인합니다(실제 도구는 머지되는 대로 교체).

### 5.1 실행 도구 묶음 `AnalysisRuntime` (GLOW-129)

노드는 `config["configurable"]["analysis_runtime"]`의 묶음에서 등록표·분석 준비(`judge_no_delay`·`build_engine_input`)·`decide`(`decision_of`)·`explain`·`final_check_input`(T12 입력)·기준일·입력 순번·추적기를 꺼냅니다. 분석 준비가 엔진 입력을 한 번 담고(`start`), 뒤 노드는 `runtime.call(도구, 입력, step=…)`으로 `call_tool`을 부릅니다. 자세한 것은 [그래프 문서](delay-analysis-graph.md) 5절 "실행 도구 묶음".

- 개발 1·3 함수의 실제 타입을 import하지 않고 모양(Protocol)만 씁니다: `EngineHoldLike(code, message)`, `ExplanationFailedLike(model_name, prompt_version, failure_summary())`.
- 엔진 대조가 다르거나 최종 확인(T12)에서 떨어지면 노드가 `code_error`로 `FAILED`(`UNEXPECTED_TOOL_ERROR`, `VALIDATING_OUTPUT`)를 올립니다.
- **도구 밖 함수의 예외(이슈 #57 1번, 결정 6):** 분석 준비·`decide`·`explain`·`final_check_input`·STALE 조회처럼 등록표를 거치지 않는 함수가 예외를 내도, 노드 래퍼(`graph.with_failure_stop`)가 `FAILED`·`UNEXPECTED_TOOL_ERROR`·시도 1·소진 아님 실패 요약으로 바꿉니다. 실패 단계는 그 노드의 단계(조정 에이전트는 재고 영향 결과 전·NO_RISK 판정까지 `ANALYZING_IMPACT`, 그 뒤 `ANALYZING_OPTIONS`)이고, 문구는 고정(`UNEXPECTED_ERROR_MESSAGE`)이며 예외 내용은 서버 로그로만 보냅니다. 실행 도구 묶음 연결 실수(`RuntimeWiringError`: 묶음 없음·다른 형식·실행 둘에 씀·분석 준비 전 도구 문맥)는 부르는 코드의 버그라 바꾸지 않고 올립니다.

### 5.2 추적 연결 (GLOW-124)

| 곳 | 연결 |
| --- | --- |
| 도구 시도 | `call_tool`이 `on_attempt=ctx.tracer.tool_attempt`로 감쌈 |
| 그래프(노드·갈림길·시작·종료) | 그래프 실행 `config["callbacks"] = [tracer.callback_handler()]` |
| 설명 시도(모델 호출 수·토큰, NFR-008) | 설명 함수가 `ctx.tracer.explanation_attempts(attempts, usage)` |
| 실행 끝 | `try/finally` 안에서 `await tracer.aclose()` (동기 `close()`는 이벤트 루프를 최대 2초 멈춤, #42 리뷰 반영·DV1-4) |

## 6. 결정 사항

| # | 내용 | 상태 |
| --- | --- | --- |
| 1 | 분석 도중 보류 | **확정(11-05, 세 세션):** 보류는 엔진 입력 단계(`EngineHold`)에서만. 엔진도 보류를 `snapshot.build`에서만 냄(`run_s1_s5.py` `analyze_run`), 명세 4.5 ON_HOLD 행·#28 `FailureSummary`("ON_HOLD는 VALIDATING만")와 같음. 도구의 `RequiredDataError`는 FAILED |
| 2 | 판정 서명·의존 범위 | **확정:** 개발 1 `decision_of(ctx.engine)`(엔진 `analysis`의 `scope`·`signature`·`sha`). 노드가 T11 뒤 한 번 부름 |
| 3 | 조회 도구 T2~T5·T8 입력 | **확정:** 입력 없음(`payload: None`) |
| 4 | 추적 설정 오류(`TraceConfigError`, LangChain 자동 추적 환경 변수가 켜져 있으면 냄) 때 | 대기. 추천: 추적 없이 분석을 계속하고 서버 로그에 경고(GLOW-124 "추적 장애가 분석에 영향 없음") |
| 5 | 설명 자산·모델 설정 오류(`SystemConfigError`) 때 | 대기. 추천: 서버 시작 때 한 번 확인해 막음(GLOW-127 PR이 `engine_startup.verify_engine_on_startup()`에 한 줄, `GLOWSHIP_ENGINE_DIR` 없으면 건너뜀). 실행 도중이면 FAILED `UNEXPECTED_TOOL_ERROR` |
| 6 | 도구 밖 함수의 예외(이슈 #57 1번, #55 리뷰 후속) | **확정(사용자 11-07):** 노드에서 실패 요약으로 바꿈(5.1절). 서비스에서만 잡으면 그래프 상태가 사라져 실패 단계를 정확히 남길 수 없고 계산 결과도 잃기 때문입니다(agentflow 7.1: 실패 요약 5칸은 에이전트 실행 코드가 만듦). 코드는 agentflow 7.1 "분류되지 않은 오류" `UNEXPECTED_TOOL_ERROR`를 그대로 씁니다(새 코드 없음). 그래프 밖으로 나가는 나머지(연결 실수 `RuntimeWiringError`, LangGraph 자체 오류)는 자동 시작(#51)이 마지막에 잡아 실행을 진행 중으로 남기지 않습니다 |

- 계약 문서 9절의 새 결정 번호: A가 Q13~Q15를 더했으므로 세 개발 세션의 새 결정은 **Q16부터** 씁니다(9절은 개발 2 몫).
- **확인할 것(개발 3):** 설명 함수가 통과시킨 설명이 최종 확인(T12)에서 떨어지면 지금은 연결 오류 `FAILED`(`UNEXPECTED_TOOL_ERROR`)로 멈춥니다. "3회 실패"(`EXPLANATION_GENERATION_FAILED`)는 DDL ck26이 3회·소진만 허용하고 실제 시도 수와 달라 쓰지 않았습니다. 계산 결과까지 저장할지는 실제 T12를 연결할 때 개발 3과 정합니다.
- 참고: #43(T9)의 9절 Q13은 "영향 SKU가 T9 입력 `skus[]`에 없으면 `RequiredDataError`로 실행 보류"입니다. 위 결정 1과 달라, 그래프에서는 FAILED로 처리합니다(엔진은 `sku_attr`를 고객사 전체 SKU로 만들어 데이터로는 생기지 않는 입력 연결 오류).

## 7. 그래프 결과 → 저장

| 그래프 종료 | 저장 함수 | 넘기는 것 |
| --- | --- | --- |
| READY_FOR_REVIEW·NO_RISK | `save_analysis_result` | `AnalysisResult`(T6·T7·T10·T11 출력, `run_basis_from_tools(…)`, `decision_of` 결과, READY면 `Explanation`) |
| 설명 3회 실패 | `save_analysis_failure` | `explanation_failure(시도 기록)`, 계산 결과(설명 없이), `ExplanationModel`. 의존 범위는 비우고 판정 서명은 저장 |
| 도구 실패 | `save_analysis_failure` | `ToolFailure.summary` |
| 도구 밖 함수 예외(결정 6) | `save_analysis_failure` | 상태의 `failure`(노드 래퍼가 만든 `UNEXPECTED_TOOL_ERROR` 요약). 도구 실패와 같은 길 |
| 그래프 밖으로 나간 예외(연결 실수·LangGraph 자체 오류) | `save_analysis_failure` (자동 시작 #51이 할 일) | 마지막 잡기: 실행을 진행 중으로 남기지 않음. 그래프 상태가 없어 실패 단계는 #51에서 정함 |
| 엔진 입력 보류 | `save_analysis_failure` | `hold_summary(code, message)` |
| NO_DELAY | `save_no_delay` | `judge_no_delay` 결과의 판정 서명·SHA-256 |
| 노드 경계 STALE | 저장 안 함 | 상태는 서비스가 이미 STALE로 바꿈 |

## 8. 작업 중 실패와 원인

- **노드가 직접 낸 취소를 그대로 올라온다고 짐작함(이슈 #57 1번):** 시험에서 설명 함수가 `asyncio.CancelledError`를 던지면 그대로 올라올 것으로 보았지만, LangGraph(`pregel/_retry.py`)는 노드 안에서 생긴 취소를 `NodeCancelledError`로 바꿔 올립니다. 확인하려던 것은 "밖에서 실행을 취소하면 실패 요약으로 삼키지 않는다"라서, 실제 취소(`task.cancel()`)로 시험을 바꿨습니다.

- **음성 시험이 적용되지 않은 채 통과로 보일 뻔함:** "보류 → FAILED 바꿈 빼기" 변경이 ruff가 줄 모양을 바꿔 적용되지 않았는데 시험은 그대로 통과했습니다. 적용 여부를 출력으로 확인해 다시 했고, 그때는 해당 시험이 실패했습니다.
- **가짜 출력이 다음 도구 입력에서 다시 검사됨(GLOW-129 노드 연결):** Pydantic은 이미 만든 모델을 다른 모델에 넣을 때 모델 뒤 검사(`model_validator(mode="after")`)를 다시 돌립니다. 도구가 내는 출력은 다음 도구 입력에 들어가므로, 도구 출력은 계약 모델의 뒤 검사를 통과해야 합니다(가짜 도구도 마찬가지). 자세한 것은 [그래프 문서](delay-analysis-graph.md) 9절.
