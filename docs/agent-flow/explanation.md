# AI 설명 생성·검증 (GLOW-127)

분석이 `READY_FOR_REVIEW`로 끝난 실행에 사용자용 설명(`explanation.v1`)을 만들고 검증합니다. 설명은 계산 결과를 바꾸거나 새 수치를 만들지 않고, 계산 결과를 근거 표지(`[E1]`)와 함께 문장으로 옮깁니다(agentflow 4절, MVP "AI 수치 정확성").

- 코드
  - 설명 생성: `app/modules/delay_analysis/application/agent/explanation.py`
  - T12 검증 도구: `application/agent/tools/output_validation.py`(모델 `validation_schemas.py`, 계약 `tool-contracts.md` 8절)
  - 설명 자산 확인: `infrastructure/engine/explanation_assets.py`
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
| 추적 | `tracer.explanation_attempts(attempts)`(GLOW-124). 추적기가 망가져도 결과는 같음 | 시도 기록(원문·canary 없음) |

검증 규칙을 서버에서 다시 만들지 않은 이유: 규칙이 많고(스키마, 근거 경로 되읽기, 문장의 정수·날짜 다중집합 대조, 금지 표현 허용 규칙, 유출, 길이, 추천 일치) 데이터 팀이 정답 묶음과 함께 검증한 구현이 있습니다. 서버에서 새로 쓰면 두 구현이 어긋날 수 있고, 어긋났는지 확인하는 비용이 이식 비용보다 큽니다. 계산 엔진을 로더로 불러 쓰는 것(GLOW-114)과 같은 방향입니다.

## 2. 함수 경계

```python
async def generate_explanation(*, snap, analysis, analysis_run_id: str, shipment_code: str,
                               now: datetime, adapter=None, tracer=None) -> ExplanationPassed | ExplanationFailed
```

- **`snap`·`analysis`:** 엔진 입력 스냅샷과 `pipeline.analyze` 결과입니다. GLOW-125 `EngineInput.snap`·`analysis`를 그대로 넘깁니다. 인터페이스 초안(`agent-tool-interface`)의 `generate_explanation(ctx, *, now)`는 `ctx.engine`에서 이 둘을 꺼내 이 함수를 부르는 몇 줄짜리 감싸기가 됩니다. `ToolContext`·`EngineInput`이 아직 develop에 없어서, 먼저 명시적인 인자로 만들었습니다.
- **`shipment_code`:** 설명 첫 문장의 지연 선적 코드입니다. 엔진 하네스는 저장소의 선적 표에서 꺼냅니다. 서버에서는 GLOW-125 `EngineInput.store`의 선적 표(`store.ships[shipment_id]["shipment_code"]`, 엔진 하네스와 같음)에서 꺼낼 예정입니다(6절).
- **`adapter`:** 기본은 엔진 `MockAdapter`입니다(라벨은 해시를 대조한 자산에서 읽음). 실제 외부 모델 호출은 승인되지 않았고, 엔진 `ExternalModelAdapter`는 부르면 거부합니다. 모델을 바꿀 때는 이 자리에 같은 `complete(prompt, request)` 모양의 어댑터를 넣고, 자산 등록부에 모델 설정을 등록합니다.

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
- 모델 설정 `mock-template-v1`

엔진 `assets.load`가 등록부 `registry_v2.json`의 SHA-256과 파일을 하나씩 대조합니다. 파일이 없거나 해시가 다르면 `SystemConfigError`이고, 서버는 `ExplanationAssetError`(같은 `code`)로 바꿔 올립니다.

- **시도로 세지 않음:** 모델 호출 전 시스템 구성 오류라서 설명 시도로 세지 않고, 실행 상태도 바꾸지 않습니다(엔진 generator 머리말, 사용자 확정 4항). 모델 호출 0번, 추적 기록 0건인지 시험이 확인합니다.
- **시작 단계 확인:** `verify_explanation_assets()`는 엔진 경로가 있으면 대조하고, 없으면 건너뜁니다. `engine_startup.verify_engine_on_startup()`이 엔진 해시 확인 뒤에 부릅니다(#36 머지 뒤 11-06에 연결). 누락·판 어긋남이 첫 설명 때가 아니라 배포 직후에 드러나게 하려는 것입니다. 시험은 `test_explanation.py` 7절에 있습니다: 자산 오류면 서버가 뜨지 않고, 엔진 경로가 없으면 엔진 확인과 함께 건너뜁니다.
- **엔진 로더 목록과의 관계:** 엔진 로더 해시 목록(`engine_manifest.json`)에도 자산 파일이 들어 있어서, 서버가 엔진을 불러올 때 이미 한 번 대조됩니다. 자산 확인은 엔진이 정한 등록부(어떤 판을 쓰는지)까지 맞는지 보는 두 번째 확인입니다.

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
2. **입력 받는 곳:** `EngineInput`(GLOW-125 2부)이 머지되면 `ctx.engine.snap`·`analysis`에서 부르는 감싸기를 붙입니다. 지연 선적 코드는 `EngineInput.store`의 선적 표에서 꺼냅니다(2절).
3. **overlay 회귀:** overlay READY 실행의 저장 설명과 다시 만든 설명이 같은지, 설명 실패 실행의 시도 기록이 같은지 대조합니다. 엔진 입력이 DB에서 만들어진 뒤(2번)에 할 수 있습니다. overlay는 엔진 시도 기록만 저장하고, #28 문구는 "안내 + (시도: 기록)"이라서 시도 기록 부분만 대조합니다.
4. ~~시작 단계 확인 연결~~ 11-06 완료(3절).
5. **"근거·가정·미확인 정보 구분"(Jira 인수 조건):** `explanation.v1`(명세 7절 U10)에는 이 구분 필드가 없습니다. 지금은 명세대로 다룹니다.
   - 경고 목록(`warnings`)
   - "예측 기준" 문구 필수(없으면 `SCHEMA_INVALID`)
   - 확정·보장 표현 금지(`FORBIDDEN_EXPRESSION`)
   - 필드를 더하려면 스키마 판을 올려야 하고, 이는 데이터 팀·설계 결정입니다.
6. **`tools/__init__.py` 머리말:** "T12는 아직 모델이 없다"는 머리말이 남아 있습니다. A의 #43·#44가 같은 파일을 고치고 있어서 충돌을 피하려고 이번에는 고치지 않았습니다. 그 두 PR이 머지된 뒤 한 줄 고칩니다.
7. **실제 모델 연결 전 `location` 정리(#45 리뷰):** 출력에 `explanation.v1`에 없는 키가 있으면, 엔진 스키마 검사가 그 키 이름을 위치(`location`)에 그대로 넣습니다. 예를 들어 키가 이메일이면 `SCHEMA_INVALID@/<그 키>`가 됩니다.
   - 위치는 `failure_message`와 추적으로 이어지므로, "원문을 넣지 않는다"는 규칙과 어긋납니다.
   - mock은 이런 출력을 만들지 않아 지금은 생기지 않습니다.
   - 실제 모델을 붙이기 전에 데이터 팀과 정리합니다. 엔진 검사가 키 이름 대신 고정 문구를 쓰게 하거나, 서버가 위치를 걸러 냅니다. 서버에서 거르면 계약이 바뀝니다.
8. **추천안 없는 READY 실행(#45 리뷰):** 입력 문서에 추천안이 없으면 엔진 검증기가 `KeyError`를 냅니다. 지금은 `KEEP_PLAN`이 늘 `FEASIBLE`이라 READY 실행에는 추천안이 항상 있습니다(계약 7절). 엔진의 추천 규칙이 바뀌면 함께 확인합니다.

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
