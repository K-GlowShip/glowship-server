# AI 설명 생성·검증 (GLOW-127, 실제 모델 연결 준비 GLOW-150)

분석이 `READY_FOR_REVIEW`로 끝난 실행에 사용자용 설명(`explanation.v1`)을 만들고 검증합니다. 설명은 계산 결과를 바꾸거나 새 수치를 만들지 않고, 계산 결과를 근거 표지(`[E1]`)와 함께 문장으로 옮깁니다(agentflow 4절, MVP "AI 수치 정확성").

- 코드
  - 설명 생성: `app/modules/delay_analysis/application/agent/explanation.py`
  - T12 검증 도구: `application/agent/tools/output_validation.py`(모델 `validation_schemas.py`, 계약 `tool-contracts.md` 8절)
  - 설명 자산 확인: `infrastructure/engine/explanation_assets.py`
  - 설명 모델 고르기·어댑터: `infrastructure/explanation_models/`, 공용 모델 호출 `app/core/llm.py`(8절·9절)
- 기준: 데이터 팀 엔진 `glowship_engine/explain/`
  - `generator.py`: 생성기
  - `validator.py`: 검증기
  - `input_doc.py`: 설명 입력 문서
  - `mock.py`: 결정적 mock 모델
  - `assets.py` + `assets/registry_v2.json`: 자산·등록부
  - 하네스 `run_s1_s5.py` `explain_run`

## 1. 무엇을 서버에서 하고 무엇을 엔진에 맡기나

엔진 `generator.generate(입력 문서, 모델, 생성 시각)`이 다음을 이미 다 합니다.
- 자산 사전 검사
- 최대 3시도(모델 호출 1회 + 검증 1회 = 1시도)
- 이전 실패 사유를 다음 시도에 넘기기(원문은 넘기지 않음)
- 실패 원문 버리기
- 시도 기록과 실패 문구 `1:코드@위치|…;2:…;3:…` 만들기

서버는 이 함수를 **그대로 부르고**, 앞뒤만 붙입니다.

| 단계 | 서버가 하는 일 | 엔진 |
| --- | --- | --- |
| 입력 문서 | `input_doc.build(snap, analysis, 실행 ID, 지연 선적 코드)`를 부름. `READY_FOR_REVIEW`가 아니면 모델을 부르지 않고 `ValueError` | 문서 내용(코드·정수·ISO 날짜·사유 코드만, 사용자 정보·자유 텍스트 없음) |
| 생성 시각 | 호출자가 준 `now`를 UTC 초(`YYYY-MM-DDTHH:MM:SSZ`)로 바꿈. 시간대 없는 시각은 거부 | 모델이 만든 시각을 쓰지 않음 |
| 생성·검증 | `asyncio.to_thread`로 `generate`를 부름(동기 함수, 이벤트 루프를 막지 않게) | 3시도, 검증 규칙 전부 |
| 결과 | 통과 → `ExplanationPassed`, 실패 → `ExplanationFailed` | `status`·`explanation`·`attempts`·`failure_message` |
| 추적 | `tracer.explanation_attempts(attempts, usage)`(GLOW-124). 추적기가 망가져도 결과는 같음. `usage`는 실제 모델 어댑터가 모은 시도별 사용량(mock은 없음, 8.3절) | 시도 기록(원문·canary 없음) |

검증 규칙을 서버에서 다시 만들지 않은 이유: 규칙이 많고(스키마, 근거 경로 되읽기, 문장의 정수·날짜 다중집합 대조, 금지 표현 허용 규칙, 유출, 길이, 추천 일치) 데이터 팀이 정답 묶음과 함께 검증한 구현이 있습니다. 서버에서 새로 쓰면 두 구현이 어긋날 수 있고, 어긋났는지 확인하는 비용이 이식 비용보다 큽니다. 계산 엔진을 로더로 불러 쓰는 것(GLOW-114)과 같은 방향입니다.

## 2. 함수 경계

```python
async def generate_explanation(*, snap, analysis, analysis_run_id: str, shipment_code: str,
                               now: datetime, adapter=None, tracer=None) -> ExplanationPassed | ExplanationFailed
```

- **`snap`·`analysis`:** 엔진 입력 스냅샷과 `pipeline.analyze` 결과입니다. GLOW-125 `EngineInput.snap`·`analysis`를 그대로 넘깁니다. 그래프는 이 함수를 직접 부르지 않고, 아래 감싸기 `make_explainer()`가 만든 `explain(ctx, now)`를 부릅니다(2.1절, 11-08).
- **`shipment_code`:** 설명 첫 문장의 지연 선적 코드입니다. 엔진 하네스는 저장소의 선적 표에서 꺼냅니다. 서버도 GLOW-125 `EngineInput.store`의 선적 표 `store.ships[str(shipment_id)]["shipment_code"]`에서 꺼냅니다(엔진 하네스와 같음, 열쇠는 문자열 ID, `delayed_shipment_code`).
- **`adapter`:** 넘기지 않으면 설정 `EXPLANATION_MODEL`이 고른 모델의 새 어댑터를 설명 한 건마다 만듭니다(`default_adapter()`). 기본은 엔진 `MockAdapter`입니다(라벨은 해시를 대조한 자산에서 읽음). 실제 모델은 자산 등록부에 모델 설정이 있어야 하고, 켜는 것은 승인 뒤입니다(8절). 시험이 아니면 넘기지 않습니다.

### 2.1 그래프 연결 (11-08, GLOW-129 `AnalysisRuntime`)

```python
make_explainer(*, adapter=None) -> explain(ctx: ToolContext, now: datetime)   # AnalysisRuntime.explain
final_check_input(passed: ExplanationPassed) -> ValidateAgentOutputInput     # AnalysisRuntime.final_check_input
```

- **`explain(ctx, now)`:** `ctx.engine`의 `snap`·`analysis`, `str(ctx.analysis_run_id)`, 저장소 선적 표의 지연 선적 코드로 `generate_explanation`을 부릅니다. 엔진 계산은 다시 하지 않습니다. `now`는 실행 묶음의 시계(`runtime.clock()`)입니다.
- **연결 오류(`ExplanationInputError`, 모델을 부르기 전):** `ctx.engine.analysis_run_id`가 `ctx.analysis_run_id`와 다르거나, 선적 표에 지연 선적 코드가 없을 때입니다. 설명 실패(3회)가 아니라 코드·데이터 연결 문제라 시도로 세지 않습니다.
- **추적:** `ctx.tracer`가 `explanation_attempts`를 가질 때만 넘깁니다(GLOW-124 `RunTracer`는 가짐). `ToolContext`가 정한 모양(`tool_attempt`만)이면 넘기지 않습니다.
- **`final_check_input`:** 통과 설명의 JSON을 다시 문자열로 만든 출력 + `expected_metadata()` + `canary=None`(시도마다 엔진이 만들고 버려 알 수 없음, 계약 8절). 같은 설명이면 엔진 생성기 안 검증과 판정이 같습니다(시험으로 확인).
- **실행 묶음에 끼우기:** 실제 서비스가 `AnalysisRuntime(explain=make_explainer(), final_check_input=final_check_input, …)`과 T12 등록(`ToolSpec(pure_tool(validate_agent_output), ValidateAgentOutputInput, AgentOutputValidation)`)을 하는 것은 "실제 도구 교체" 작업입니다(인터페이스 문서 5절). 여기서는 함수만 둡니다.
- 시험: `tests/modules/delay_analysis/test_explanation_runtime.py` 16건(직접 호출과 같음, 선적 코드·실행 ID 연결 오류는 모델 0회, 추적, T12 입력, 실제 설명·결과 검증 노드로 READY·FAILED). 변이 6건(선적 코드 고정·실행 ID 확인 빼기·추적기 항상 넘김·canary 넣음·UUID 열쇠 허용·출력 원문 비움)을 모두 잡았습니다.

결과는 저장 모양에 맞춰 둡니다.

| 결과 | 필드 | 저장 연결 |
| --- | --- | --- |
| `ExplanationPassed` | `model_name`·`prompt_version`·`final_explanation_json`·`generated_at` | 결과 저장(GLOW-128 #38) `Explanation`의 네 필드와 같은 이름·형식 |
| | `input_document`·`attempts`, `expected_metadata()` | T12로 다시 검증할 때 씀(4절) |
| `ExplanationFailed` | `model_name`·`prompt_version` | 실패 저장(GLOW-128 실패·보류 저장)의 `ExplanationModel` |
| | `failure_message`, `failure_summary()` | `failure_summary()` = #28 `explanation_failure(failure_message)` → `FAILED`·`VALIDATING_OUTPUT`·`EXPLANATION_GENERATION_FAILED`·3회·재시도 소진. 계산 결과는 남습니다 |

## 3. 설명 자산 확인

자산은 다섯 가지입니다(엔진 `explain/assets/`).
- 프롬프트 `explain_ready_v1@1.0.0`
- 스키마 `explanation.v1`
- 금지 표현 `ko-v2`
- 라벨 `ko-v1`
- 모델 설정 `mock-template-v1`(실제 모델을 고르면 그 모델의 설정도 시작 단계에서 확인, 8.5절)

엔진 `assets.load`가 등록부 `registry_v2.json`의 SHA-256과 파일을 하나씩 대조합니다. 파일이 없거나 해시가 다르면 `SystemConfigError`이고, 서버는 `ExplanationAssetError`(같은 `code`)로 바꿔 올립니다.

- **시도로 세지 않음:** 모델 호출 전 시스템 구성 오류라서 설명 시도로 세지 않고, 실행 상태도 바꾸지 않습니다(엔진 generator 머리말, 사용자 확정 4항). 모델 호출 0번, 추적 기록 0건인지 시험이 확인합니다.
- **시작 단계 확인:** `verify_explanation_assets()`는 엔진 경로가 있으면 대조하고, 없으면 건너뜁니다. `engine_startup.verify_engine_on_startup()`이 엔진 해시 확인 뒤에 부릅니다(#36 머지 뒤 11-06에 연결). 누락·판 어긋남이 첫 설명 때가 아니라 배포 직후에 드러나게 하려는 것입니다. 시험은 `test_explanation.py` 7절에 있습니다: 자산 오류면 서버가 뜨지 않고, 엔진 경로가 없으면 엔진 확인과 함께 건너뜁니다.
- **엔진 로더 목록과의 관계:** 엔진 로더의 런타임 해시 목록(`engine_manifest.json`)에도 자산 파일이 들어 있어서, 서버가 엔진을 불러올 때 이미 한 번 대조됩니다. 자산 확인은 엔진이 정한 등록부(어떤 판을 쓰는지)까지 맞는지 보는 두 번째 확인입니다.

## 4. T12 `validate_agent_output`

엔진 `validator.validate`를 감싼 동기 순수 함수입니다. 입출력과 판정(`PASSED`·`RETRY`·`FAILED`)은 계약 8절에 있습니다.

- **생성 중에는 쓰지 않음:** 설명을 생성하는 동안의 시도별 검증은 엔진 `generate`가 안에서 같은 `validate`를 부릅니다. T12를 따로 둔 이유는 그래프의 결과 검증 노드(agentflow "결과 검증")가 최종 설명을 저장 전에 다시 확인할 수 있게 하기 위해서입니다. 이때 `ExplanationPassed.expected_metadata()`를 씁니다.
- **기대 메타데이터를 따로 만드는 이유:** 설명 출력의 메타데이터를 믿으면 검사가 자기 자신과 비교하는 셈이 됩니다. 그래서 입력 문서·시도 기록·생성 시각에서 따로 만듭니다.
- **canary를 모를 때:** 다시 검증할 때는 그 시도의 canary를 알 수 없어서 `null`로 넘깁니다. 이때는 canary 유출 검사 하나만 빠집니다. 생성 중 검증은 엔진이 canary를 넣어 했습니다.
- **연결 오류:** 기대 메타데이터의 실행 ID·판정 서명이 입력 문서와 다르면 검증 결과가 아니라 입력 모델 오류입니다. 다른 실행의 설명을 검증하는 실수를 막습니다.

## 5. 시험과 검증

| 확인 | 시험 |
| --- | --- |
| 엔진과 같은 결과 | `test_explanation.py`: 다음이 엔진 `generate`를 직접 부른 결과와 같은지 봄 — GS-05 통과 결과(설명·모델·프롬프트·시도 기록), 결함 18종 × 3회 실패의 실패 문구·시도 기록, 무작위 READY 실행 20건 이상의 설명. GS-05 설명은 엔진 `test_explain` 골든 문장과 글자까지 같음 |
| 3번 실패 | RT-2 주입(엔진 하네스가 overlay 설명 실패 실행을 만든 방식) → 시도 기록 문자열 고정값, `failure_summary()`가 #28 모양 |
| 원문 남기지 않음 | 이메일·canary 유출, JSON 아님 결함의 실패 결과에 원문·canary가 없음 |
| 시도가 아닌 경우 | 등록 안 된 모델 설정·제공자 불일치 → 모델 호출 0번·추적 0건. `READY_FOR_REVIEW`가 아닌 상태, 시간대 없는 시각 → 모델 호출 0번 |
| 추적 | 추적기가 시도 기록을 그대로 받음. 추적기가 예외를 내도 결과가 같음 |
| 자산 확인 | 자산 복사본에서 다음 각 경우에 멈추는지 봄 — 다섯 파일 각각 1바이트 바꿈, 프롬프트 삭제, 등록 안 된 모델 설정. 엔진 경로가 없으면 건너뜀 |
| T12 | `test_output_validation.py`: 결함 16종 × 시도 1·2·3의 오류 목록이 엔진 검증기를 직접 부른 결과와 같음. 판정은 시도 1·2면 `RETRY`, 3이면 `FAILED`. 출력 메타데이터를 믿지 않음. 입력 모델이 연결 오류·값 모양을 거부함. 계약 8절 표 4개 = 모델·열거형. 사유 코드 = 엔진 소스의 코드 |
| 엔진 자체 시험 | `test_engine_loader.py`가 엔진 `test_explain` 97건을 이미 돌립니다(여기서 되풀이하지 않음) |

엔진 경로(`GLOWSHIP_ENGINE_DIR`)가 없으면 엔진이 필요한 시험은 **건너뜀**으로 나옵니다. 통과가 아니므로 결과 줄의 건너뜀 수를 봅니다.

## 6. 남은 일

1. **노드 연결:** 그래프 explain·validate 노드 연결은 GLOW-129(노드 연결)입니다. 노드 순서는 T11 → `decision_of` → explain → validate입니다. 등록표의 `(ctx, payload)` 감싸기도 그 작업에서 합니다.
2. ~~입력 받는 곳~~ 11-08 완료(2.1절 `make_explainer`·`final_check_input`). 남은 것: 실제 실행 묶음에 끼우기("실제 도구 교체" 작업).
3. ~~overlay 회귀~~ 11-08 완료: `tests/modules/delay_analysis/test_explanation_overlay.py`. 개발 DB overlay 실행을 `build_engine_input`(읽기만)으로 다시 만들고 `make_explainer`로 설명을 다시 만들었습니다. 설명이 저장된 14건(통과 13 — `SKU-005` 같은 코드 포함, 설명 실패 1 — RT-2 결함과 같은 시도 기록) 모두 `final_explanation_json`·생성 시각·모델·프롬프트 판(실패는 `failure_message`)이 저장값과 같았습니다. 기본은 2건, 전체는 `GLOWSHIP_ENGINE_GOLDEN=1`(약 2분).
4. ~~시작 단계 확인 연결~~ 11-06 완료(3절).
5. **"근거·가정·미확인 정보 구분"(Jira 인수 조건):** `explanation.v1`(명세 7절 U10)에는 이 구분 필드가 없습니다. 지금은 명세대로 다룹니다.
   - 경고 목록(`warnings`)
   - "예측 기준" 문구 필수(없으면 `SCHEMA_INVALID`)
   - 확정·보장 표현 금지(`FORBIDDEN_EXPRESSION`)
   - 필드를 더하려면 스키마 판을 올려야 하고, 이는 데이터 팀·설계 결정입니다.
6. ~~`tools/__init__.py` 머리말~~ 11-06 완료(#43·#44 머지 뒤 T12 모델·구현 두 줄로 바꿈).
7. **실제 모델 연결 전 `location` 정리(#45 리뷰):** 출력에 `explanation.v1`에 없는 키가 있으면, 엔진 스키마 검사가 그 키 이름을 위치(`location`)에 그대로 넣습니다. 예를 들어 키가 이메일이면 `SCHEMA_INVALID@/<그 키>`가 됩니다.
   - 위치는 `failure_message`와 추적으로 이어지므로, "원문을 넣지 않는다"는 규칙과 어긋납니다.
   - mock은 이런 출력을 만들지 않아 지금은 생기지 않습니다.
   - 실제 모델을 붙이기 전에 데이터 팀과 정리합니다. 엔진 검사가 키 이름 대신 고정 문구를 쓰게 하거나, 서버가 위치를 걸러 냅니다. 서버에서 거르면 계약이 바뀝니다.
8. **추천안 없는 READY 실행(#45 리뷰):** 입력 문서에 추천안이 없으면 엔진 검증기가 `KeyError`를 냅니다. 지금은 `KEEP_PLAN`이 늘 `FEASIBLE`이라 READY 실행에는 추천안이 항상 있습니다(계약 7절). 엔진의 추천 규칙이 바뀌면 함께 확인합니다.
9. **구분자 없는 SKU 코드면 설명이 실패함(11-06 GLOW-58 1단계에서 발견, 11-07 범위 바로잡음, 엔진 몫):** 엔진 검증기의 토크나이저(`explain/textcheck.tokens`)가 `CREAM20` 처럼 구분자 없이 숫자가 붙은 코드 안의 숫자를 수치로 세어, "SKU별 지연 증분은 CREAM20 100개입니다" 문장이 인용 값과 달라 `VALUE_MISMATCH@risk_summary#s2`가 됩니다. 평가 시나리오 9건 중 GS-05(`X`·`Y`)만 통과합니다. `SKU-001` 처럼 `-`·`_` 가 있는 코드는 코드 토큰으로 빠져 맞게 셉니다(실제 DB 코드 모양, overlay 의 PASSED 설명 13건). 11-06 판의 "실제 DB 코드도 해당"은 틀렸습니다. 서버 실패 흐름(#28)은 맞게 돕니다. 자세한 내용과 제안은 `docs/analysis/evaluation-scenarios.md` 4.1절. 엔진이 고쳐지면 `tests/modules/delay_analysis/evaluation/test_evaluation_explanation.py`의 xfail 표시를 지웁니다.

## 7. 작업 중 실패와 원인

- **난수 canary 때문에 시험이 가끔 실패함(11-06):** 처음 207건이 통과한 뒤 전체 시험에서 `leak_canary` 결함 비교가 1번 실패했습니다. 서버 감싸기 쪽 시도 1에만 `LEAKAGE_DETECTED@run_summary:LK-05`가 더 붙었습니다.
  - **원인:** 엔진 생성기는 시도마다 `secrets.token_hex`로 canary를 만듭니다. 그 16진 숫자가 우연히 금지 표현 사전의 전화번호 패턴(LK-05)에 맞으면, 유출된 canary가 전화번호로도 잡힙니다. 사전 내용은 공개 저장소에 적지 않습니다(#45 리뷰).
  - **원인이 이것뿐인지 확인한 방법:**
    - 같은 canary를 주면 결함 18종 × 30번(540번) 모두 감싸기와 엔진 직접 호출이 같았습니다.
    - canary만 바꾸면 결과가 달라지는 결함은 `leak_canary` 하나였고, 더 붙는 코드는 LK-05 하나였습니다.
    - 엔진에서 걸린 비율은 1,000번 중 7번(0.70%)이고, 정규식만으로 계산한 비율 0.75%와 같은 수준입니다.
  - **운영 영향 없음:** canary는 출력에 유출됐을 때만 출력에 나타나고, 그때는 이미 `LEAKAGE_DETECTED@canary`로 실패합니다.
  - **고친 것:** 시험에서만 canary를 고정했습니다(`gs05` fixture, 어떤 LK 패턴에도 맞지 않는 `ab…`). 고정이 걸렸는지 보는 시험도 더했습니다.
  - **고친 뒤 확인:** 고정을 뺀 변이에서는 비교 시험이 60번 중 3번 실패하고 고정 확인 시험은 바로 실패했습니다. 고정을 넣은 뒤에는 25번 반복해 모두 통과했습니다.

- **엔진 소스에서 사유 코드 모으기:** 처음에는 생성기 소스에서 `"MODEL_…"`을 모두 모았습니다. 그랬더니 자산 오류 코드 `MODEL_CONFIG_MISMATCH`까지 들어와 시험이 실패했습니다. 이 코드는 `SystemConfigError`의 코드이고 시도 기록에는 나오지 않습니다. 지금은 시도 기록에 붙는 `("MODEL_…", "adapter")` 모양만 모읍니다.
- **계약 문서 코드 검사:** 계약 문서의 코드값 검사(`test_tool_schemas.py` `test_doc_codes_are_known`)는 문서에 나온 대문자 코드가 모두 서버 열거형에 있는지 봅니다. 8절에 T12 판정·사유 코드를 적자 이 시험이 실패했습니다. 그래서 검사가 아는 코드에 T12 열거형 두 개를 더했습니다. 열거형이 아닌 `EXPLANATION_GENERATION_FAILED`(#28 `retry.py` 상수)는 `NON_ENUM_CODES`에 출처와 함께 더했습니다.

## 8. 실제 언어 모델 연결 준비 (GLOW-150)

설명 모델을 설정으로 고를 수 있게 서버 쪽을 준비했습니다. **기본은 mock이고, 이 작업만으로는 실제 모델이 켜지지 않습니다.** 엔진 등록부에 등록된 모델 설정이 mock 하나뿐이라, 실제 모델을 고르면 서버가 시작 단계에서 멈춥니다(의도한 안전장치).

- 코드
  - 공용 모델 호출: `app/core/llm.py`
  - 설명 모델 어댑터: `infrastructure/explanation_models/adapter.py`
  - 모델 고르기·서버 시작 확인: `infrastructure/explanation_models/selection.py`
- 시험: `tests/core/test_llm.py`, `tests/modules/delay_analysis/test_explanation_models.py`

### 8.1 구조

```
엔진 generator.generate ──complete(prompt, request)──▶ 설명 모델 어댑터 ──▶ 공용 모델 호출 ──▶ LiteLLM ──▶ 제공자
        (3시도·검증)                                  (엔진 예외로 바꿈,        (재시도 없음,
                                                       총 시간 예산)            오류 두 가지)
```

- **공용 모델 호출(`complete_text`):** 제공자(OpenAI·Gemini·Claude·Bedrock) 호출은 LiteLLM 한 겹으로 하고, LiteLLM은 이 파일에서만 부릅니다. 한 제공자에서 문제가 생기면 이 파일 안에서 그 제공자만 다른 방식으로 바꿀 수 있습니다.
- **설명 모델 어댑터:** 엔진 계약(동기 `complete(prompt, request)` → JSON 원문, 속성 `provider`·`model_id`)을 지킵니다. 엔진이 만든 프롬프트에 입력 문서가 이미 들어 있어 사용자 메시지 하나로 보냅니다. `request`(입력 문서·canary)는 쓰지 않습니다.
- **설명 한 건마다 새 어댑터:** `generate_explanation`이 어댑터를 받지 않으면 건마다 `default_adapter()`를 부릅니다. 총 시간 예산과 시도별 사용량이 건별 상태라서, 동시에 도는 실행(최대 `ANALYSIS_MAX_CONCURRENT_RUNS`)이 하나를 같이 쓰면 값이 섞입니다. 실행 묶음(`engine_runtime.py`)은 `make_explainer()`를 어댑터 없이 부르므로 고치지 않았습니다.
- **엔진 `ExternalModelAdapter`는 쓰지 않습니다.** 부르면 `PermissionError`를 내는 자리 표시이기 때문입니다. 같은 계약의 서버 쪽 어댑터를 쓰는 방식과 실제 호출은 데이터 팀 승인 대상입니다(8.6절).

### 8.2 설정

| 환경 변수 | 기본 | 뜻 |
| --- | --- | --- |
| `EXPLANATION_MODEL` | `mock/mock-template-v1` | `제공자/모델 ID`. 제공자는 `openai`·`gemini`·`anthropic`·`bedrock`. 비우면 mock |
| `OPENAI_API_KEY`·`GEMINI_API_KEY`·`ANTHROPIC_API_KEY` | 비움 | 제공자마다 하나. 고른 제공자의 키만 필요. `bedrock`은 키 대신 실행 환경의 자격 증명(인스턴스 역할)을 씀 |
| `EXPLANATION_MODEL_TIMEOUT_SECONDS` | 12 | 모델 호출 한 번의 시간 제한 |
| `EXPLANATION_TOTAL_BUDGET_SECONDS` | 20 | 설명 한 건(엔진 3시도 합)의 총 시간 예산 |

- `제공자`와 `모델 ID`는 엔진 모델 설정 파일의 `provider`·`model_id`와 글자까지 같아야 합니다. 엔진이 `model_id`로 등록부의 설정을 찾고(설정 ID = 모델 ID), 제공자가 다르면 `MODEL_CONFIG_MISMATCH`로 멈춥니다. 저장되는 모델 이름도 이 값입니다(`openai/<모델 ID>`).
- 키는 `SecretStr`이고 호출할 때 직접 넘깁니다. 서버 설정은 `.env`를 읽어도 환경 변수로 내보내지 않으므로, LiteLLM이 환경 변수에서 키를 찾게 두지 않습니다.
- 두 시간 값은 초안입니다. 모델별 측정 뒤 정합니다(8.6절).

### 8.3 호출 규칙

| 규칙 | 내용 | 이유 |
| --- | --- | --- |
| 실패는 두 가지 | 시간 초과 → 엔진 `ModelTimeout`, 그 밖의 모든 것 → 엔진 `ModelCallError` | 엔진은 이 둘만 실패한 시도로 셉니다. 다른 예외는 생성기를 뚫고 나가 실행이 예기치 않은 오류로 끝납니다 |
| 재시도 없음 | LiteLLM·제공자 SDK의 재시도를 0으로 | 엔진 3시도와 곱해지지 않게(켜 두면 최악 시간이 3배) |
| 총 시간 예산 | 첫 호출부터 재고, 남은 시간이 없으면 부르지 않고 `ModelTimeout`. 호출 시간 제한도 남은 예산을 넘지 않게 줄임 | 엔진 3시도의 최악 시간을 예산 안에 묶음 |
| JSON 방식 | `response_format={"type": "json_object"}`만 요청. 스키마 강제는 안 함 | `explanation.v1`의 길이·패턴 제약을 제공자들이 그대로 받지 못함. 검증은 엔진이 함 |
| 거부·잘림·빈 응답 | 글이 있어도 호출 실패로 봄 | 반쯤 쓴 글을 검증기로 넘기지 않음 |
| 시도별 사용량 | 토큰 수·걸린 시간(실패면 사유 코드)을 추적기의 사용량 칸으로 넘김 | NFR-008. 예상 비용 계산은 "모델 사용 비용 기록" 작업에서 함 |

호출 실패 사유 코드(추적에 남는 값): `TIMEOUT`·`AUTH`(401·403)·`RATE_LIMIT`(429)·`REFUSAL`·`CONNECTION`·`SERVER`(5xx)·`BAD_REQUEST`(그 밖의 4xx)·`TRUNCATED`·`EMPTY`·`UNEXPECTED`, 그리고 예산이 다해 부르지 않은 시도의 `BUDGET_EXHAUSTED`. 엔진 시도 기록에는 `MODEL_TIMEOUT@adapter`·`MODEL_CALL_ERROR@adapter` 둘로만 남습니다.

### 8.4 남기지 않는 것

- **제공자 오류 원문:** 오류 문구에는 키·프롬프트 일부가 섞일 수 있습니다. 공용 층은 원래 예외를 버리고 사유 코드·모델 이름·상태 코드만 가진 새 예외를 올립니다(원래 예외가 새 예외에 달려 있지 않음).
- **로그:** 모델 이름·사유 코드·상태 코드·걸린 시간만 남깁니다. LiteLLM과 OpenAI SDK의 DEBUG·INFO 로그에는 프롬프트가 들어가므로 두 로거의 수준을 WARNING으로 고정합니다. 서버 로그 수준을 DEBUG로 내려도 따라 내려가지 않습니다.
- **외부 기록:** LiteLLM 콜백(LangSmith·Langfuse 같은 외부 기록 서비스로 요청·응답을 보내는 기능)이 하나라도 켜져 있으면 모델을 부르지 않습니다(`LlmConfigError`). 실행 추적은 서버 추적기(`agent-tracing.md`)가 마스킹한 값으로만 합니다.
- **원격 조회:** LiteLLM은 import할 때 가격표를 원격에서 받아 옵니다(5초 제한). `LITELLM_LOCAL_MODEL_COST_MAP=True`를 넣고 불러와 묶음에 든 표만 씁니다.

### 8.5 서버 시작 확인

`app/main.py` 시작 단계가 엔진 확인 다음에 `check_explanation_model_ready`를 부릅니다. mock이면 아무것도 하지 않습니다(모델 호출 라이브러리도 불러오지 않음). 실제 모델이면 아래 가운데 하나라도 해당하면 서버가 뜨지 않습니다.

| 경우 | 오류 |
| --- | --- |
| `GLOWSHIP_ENGINE_DIR` 없음 | `ExplanationModelConfigError` |
| 모르는 제공자, 그 제공자 키 없음, `mock/` 뒤가 `mock-template-v1`이 아님 | `ExplanationModelConfigError` |
| 모델 호출 라이브러리 없음 | `ExplanationModelConfigError` |
| 엔진 등록부에 그 모델 설정이 없음 | `ExplanationAssetError` `ASSET_NOT_REGISTERED` |
| 등록된 설정의 제공자·모델 ID가 다름 | `ExplanationAssetError` `MODEL_CONFIG_MISMATCH` |

등록부 확인은 엔진 `generator.preflight`를 그대로 부릅니다(설명을 만들 때 엔진이 하는 것과 같은 검사).

### 8.6 켜는 순서

1. 모델 호출 통신 경로를 정합니다(서버에서 제공자 API로 나갈 수 있는지, Bedrock만 되는지). 후보 모델 이름이 이 결정에 달려 있습니다.
2. 데이터 팀이 새 엔진 판에 후보 모델의 모델 설정 파일과 등록부 항목을 넣습니다(`config_id` = 모델 ID). 서버는 엔진 폴더를 바꾸고 런타임 해시 목록을 갱신합니다(`docs/analysis/engine-local-path.md` 4절).
3. 외부 전송 승인을 받습니다(제공자별). 나가는 것은 엔진 프롬프트 + 설명 입력 문서(코드·정수·날짜·사유 코드)입니다.
4. 키를 비밀값 저장소에 넣고, 모델별로 통과율·시도 수·시간·토큰을 잽니다. 그 결과로 모델과 두 시간 값을 정합니다.
5. `EXPLANATION_MODEL`과 키를 넣고 서버를 시작합니다. 시작 확인을 통과하면 다음 설명부터 실제 모델을 씁니다.

### 8.7 주의

- **실제 제공자 호출은 확인하지 못했습니다.** 시험은 이 PC의 가짜 HTTP 서버가 OpenAI·Claude·Gemini 응답 모양으로 답하게 하고 실제 LiteLLM이 그 서버를 부르는 방식입니다. 요청 횟수·오류 변환·출력은 확인했지만, 실제 모델이 프롬프트 `explain_ready_v1@1.0.0`으로 검증을 통과하는지는 키가 온 뒤에 잽니다. Bedrock 경로는 형식만 받아들이고 호출은 한 번도 해 보지 않았습니다.
- **시간 제한은 "응답이 오지 않는 시간" 기준입니다.** HTTP 호출에 거는 값이라, 조금씩 흘려 보내는 응답은 제한보다 오래 걸릴 수 있습니다.
- **모델 호출은 스레드에서 돌아 취소되지 않습니다.** 엔진 생성기가 `asyncio.to_thread` 안에서 돌기 때문입니다. 서버 종료 대기(`ANALYSIS_SHUTDOWN_GRACE_SECONDS`, 기본 10초)보다 호출 시간 제한(12초)이 길면, 종료가 그 호출이 끝날 때까지 늦어질 수 있습니다.
- **모델 장애 = 분석 실패:** 설명이 3번 실패하면 실행은 `FAILED`·`EXPLANATION_GENERATION_FAILED`입니다. 실제 모델에서는 제공자 장애·시간 초과가 여기로 이어집니다. 이 규칙을 그대로 둘지는 설계 결정입니다.
- **생성 값은 아직 넣지 않았습니다.** 최대 출력 토큰·추론 강도는 제공자·LiteLLM 기본값을 씁니다. Claude는 LiteLLM 기본 최대 출력이 4,096 토큰이라 설명(약 3,000자)이 잘릴 수 있습니다. 엔진 모델 설정 파일의 `settings` 모양을 데이터 팀과 정한 뒤 넣습니다.
- **출력의 스키마 밖 키 이름(6절 7번)** 은 실제 모델을 켜기 전에 정리해야 합니다.

### 8.8 측정 (11-11, 개발 PC Windows, Python 3.14)

| 항목 | 값 | 비고 |
| --- | --- | --- |
| 새 패키지 | 93 → 128개(+35) | `litellm==1.106.6`. 기존 패키지 판은 그대로 |
| 설치 크기 | +180MB | 새 패키지 35개의 설치 파일 합(바이트코드 제외). `litellm` 119MB, `botocore` 21MB, `hf-xet` 10MB, `tokenizers` 8MB. **mock이어도 배포 이미지는 이만큼 커집니다**(이미지는 빌드해 재지 않음, 리눅스 휠은 조금 다를 수 있음) |
| mock 서버 | 모델 호출 라이브러리를 불러오지 않음 | 시작 시간·메모리 변화 없음(시험으로 확인). 달라지는 것은 위 설치 크기뿐 |
| LiteLLM 불러오기 | 3.5~4.0초, 메모리 +139MB(147 → 286MB) | 실제 모델을 고른 서버의 시작 단계에서 한 번. 설치 직후 첫 실행은 34초(바이트코드 생성) |
| 첫 모델 호출 | 약 1.3초 추가 | LiteLLM이 처음 호출 때 제공자 모듈을 더 불러옴. 그 뒤 호출의 추가 시간은 0.01~0.03초 |

dev 서버(리눅스)에서는 값이 다를 수 있습니다. 메모리가 작은 서버에서 실제 모델을 켤 때는 엔진 저장소 캐시(`ENGINE_STORE_CACHE_MAX_ENTRIES`)와 함께 봅니다.

### 8.9 시험과 변이

| 확인 | 시험 |
| --- | --- |
| 세 제공자 응답 읽기 | 글·토큰 수·모델 이름, 요청 1번, 키는 헤더로만, 프롬프트는 사용자 메시지 하나 |
| 오류 변환·재시도 없음 | 429·401·403·500·503·400·404·연결 거부·시간 초과·잘림·거부·빈 응답 → 사유 코드, 요청 1번 |
| 새지 않음 | 제공자가 오류 본문에 키·프롬프트를 되돌려 줘도 예외 문구·로그·표준 출력에 없음(로그 수준 DEBUG에서) |
| 외부 기록·원격 조회 | 콜백이 켜져 있으면 요청 0번, 가격표는 묶음에 든 것 |
| 어댑터 | 호출 인자, 엔진 예외로 변환, 남은 예산만큼 시간 제한 줄이기, 예산이 다하면 호출 없이 실패 |
| 엔진과 함께 | 등록된 시험용 모델 → 통과(모델 이름·사용량 추적), 3번 실패 문구, 실패 뒤 통과, 예산 소진, 등록 안 된 모델은 호출 0번 |
| 건마다 새 어댑터 | 동시에 도는 설명 두 건, 실행 묶음의 `explain` 두 번 → 어댑터 2개 |
| 시작 확인 | 위 8.5절 표의 경우들 + mock은 아무것도 안 함 + 시작 단계가 자동 시작 확인보다 먼저 멈춤 |

엔진이 필요한 시험은 엔진 자산 복사본에 시험용 모델 설정을 등록해서 봅니다(엔진 원본은 그대로).

## 9. 다른 기능에서 모델을 부를 때

지연 메일 해석·고객 안내문 초안처럼 언어 모델이 필요한 다른 기능은 공용 모델 호출(`app/core/llm.py`)을 그대로 부릅니다.

```python
from app.core.llm import LlmCallError, LlmTimeoutError, api_key_for, complete_text, split_model

provider, _ = split_model(settings.<기능>_model)          # 모델 설정은 기능마다 따로 둔다
result = complete_text(
    model=settings.<기능>_model,                           # "제공자/모델 ID"
    prompt=prompt,
    api_key=api_key_for(settings, provider),               # 키는 제공자마다 하나(공용)
    timeout_seconds=10.0,
    response_format=Draft,                                 # Pydantic 모델을 넘기면 제공자의 구조화 출력 요청이 된다
)
draft = Draft.model_validate_json(result.text)             # 결과는 글. 모델로 읽는 것은 부르는 쪽이 한다
```

- **동기 함수입니다.** 요청 처리(비동기) 안에서는 `asyncio.to_thread`로 부릅니다.
- **재시도는 부르는 쪽이 정합니다.** 공용 층은 한 번만 부르고, 실패는 `LlmTimeoutError`·`LlmCallError`(사유 코드 `reason`, 상태 코드 `status_code`) 두 가지로 올립니다.
- **결과:** `text`(글), `model`(요청한 이름), `response_model`(제공자가 답한 이름), `usage`(입력·출력 토큰 수), `elapsed_ms`. 추적·비용 기록은 부르는 쪽이 자기 추적기에 넘깁니다.
- **남기지 않는 규칙(8.4절)은 공용 층에 있습니다.** 부르는 쪽도 프롬프트·모델 출력을 로그에 남기지 않습니다.
- **외부 전송 승인은 기능마다 따로 받습니다.** 설명 입력 문서에는 사용자 정보·자유 텍스트가 없지만, 메일·안내문은 다릅니다.
- **LangChain의 구조화 출력(`with_structured_output`)을 쓰려면** 의존성 `langchain-litellm`을 더하고, 같은 설정(모델 이름·키·시간 제한·재시도 0)으로 채팅 모델을 만드는 함수를 이 파일에 더합니다. 설명 경로는 고치지 않습니다. 그때 정해야 할 것: LangChain 채팅 모델은 자동 추적 환경 변수(`LANGSMITH_TRACING` 등)가 켜져 있으면 프롬프트를 LangSmith로 그대로 보냅니다. 지금 서버는 자동 추적이 켜져 있으면 자동 시작을 막으므로(`agent-tracing.md`), 추적을 서버 추적기로만 할지 다시 정해야 합니다.
