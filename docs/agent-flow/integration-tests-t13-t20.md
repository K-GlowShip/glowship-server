# 통합 시험 T13·T20 — 재시도 제한·실패 요약과 추적 장애 독립성 (GLOW-60)

연결된 분석 그래프 전체(노드 7개·도구 등록표·재시도·추적)에서 두 성질을 확인한 기록입니다.

- **T13 — 도구 재시도 제한과 실행 실패 요약**(AIR-009, agentflow 7·7.1절): 재시도는 도구 호출마다 최대 2번이고, 멈추면 실패 요약 5컬럼이 남으며, 실패한 계산을 설명으로 대신하지 않습니다.
- **T20 — 추적 장애 독립성**(AIR-008, agentflow 8.1절 "장애 영향"): 추적이 실패하거나 느려도 분석 상태·재시도 횟수·실패 요약이 바뀌지 않습니다.
- 번호 T13·T20은 업무 분장 v6.2 2절의 통합 시험 번호입니다. agentflow 8.1절 "검증 기준"이 이 시험을 요구합니다.
- 운영 코드(`app/`)는 바꾸지 않았습니다. 시험은 모두 `tests/modules/delay_analysis/`에 있습니다.

| 파일 | 내용 | 건수 |
| --- | --- | --- |
| `integration_support.py` | 공통 장치(시험 아님): 갈래 표, 호출 수를 세는 도구, 실제 설명 갈래 묶음, 추적 장애 | — |
| `test_integration_t13_retry.py` | T13 갈래별 기대값 | 40 |
| `test_integration_t13_failure_save.py` | T13 실패 요약 → 저장 → 실제 행 | 17 |
| `test_integration_t20_tracing.py` | T20 갈래 × 추적 장애, 사건 연결, 금지 항목 | 64 |

## 1. 기대값의 근거

정답 자료(`05_정답계약검증`의 정량 정답표 v2·검증 `bundle_verify.json`)에 있는 값은 3건입니다. 정답 DB에도 같은 행이 있습니다.

| 정답 | 실행 | 상태 / 단계 / 코드 / 시도 / 소진 | 그 밖 |
| --- | --- | --- | --- |
| RT-1 | E3 v2 `AN-260110-001` | FAILED / `ANALYZING_IMPACT` / `TOOL_TIMEOUT_EXHAUSTED` / 3 / true | 결과 행 없음 |
| RT-2 | E2 v1 `AN-260107-001` | FAILED / `VALIDATING_OUTPUT` / `EXPLANATION_GENERATION_FAILED` / 3 / true | 설명 검증 FAILED, 모델 `mock/mock-template-v1`, 프롬프트 `explain_ready_v1@1.0.0`, 문구 = 시도 기록, 결과 행 있음 |
| 보류 | E9 v1 `AN-260102-002` | ON_HOLD / `VALIDATING` / `REQUIRED_DATA_INVALID` / 1 / false | 문구 = 판정한 쪽 문구 |

- 권한 오류·일시 오류·분류 안 된 예외·도구 밖 예외와 추적 장애의 기대값은 정답 자료에 없습니다(정성 답안지 "실제 처리 결과는 OPEN"). 이 부분은 재시도 규칙(`docs/agent-flow/agent-retry.md` 2절)과 agentflow 7절·8.1절 문장이 근거입니다.
- 문구는 글자 전체가 아니라 뜻(도구의 화면 이름·사유·재실행 방법, 내부 함수 이름 없음)으로 확인합니다. 예상하지 못한 오류(`UNEXPECTED_TOOL_ERROR`)는 고정 문구입니다(이슈 #57 2번). 글자 전체는 단위 시험(`test_agent_retry.py`)이 봅니다.

## 2. 시험 장치

- **갈래 표 하나를 T13·T20이 같이 씁니다**(`integration_support.BRANCHES`). T13은 갈래마다 기대값을 보고, T20은 같은 갈래를 추적 장애와 함께 다시 돌려 결과가 같은지 봅니다.
- 실제 `build_analysis_graph()`·`call_tool`·`call_with_retry`를 그대로 씁니다. 바꿔 끼우는 것은 가짜 실행 도구 묶음(`fake_runtime`)의 도구·분석 준비·설명뿐입니다.
- **호출 수:** 가짜 도구 12개를 모두 "들어올 때 세는" 감싸기로 감쌉니다. 기대값은 도구 12개 전체의 정확한 수입니다. 그래서 재시도 수와 "멈춘 뒤 단계의 도구는 0번"을 한 번에 봅니다.
- **시간 초과:** `ToolSpec.timeout_seconds=0.01`과 1초 걸리는 도구로 만듭니다. 세 번 모두 0.01초에 끊기므로 시험은 1초 안에 끝나야 합니다.
- **실제 설명 갈래(엔진 필요):** GS-05 엔진 입력을 분석 준비가 돌려주게 하고, `make_explainer`·`final_check_input`·실제 T12를 끼웁니다. 기본 가짜 T10·T11은 GS-05 판정 서명과 달라 비교 노드에서 멈추므로, **엔진 판정 서명에서 만든 T10·T11 가짜**를 씁니다. `GLOWSHIP_ENGINE_DIR`이 없으면 이 갈래는 건너뜁니다.
- **저장:** 정답 실패 실행을 진행 중으로 되돌린 뒤 그래프가 만든 요약을 `save_analysis_failure`로 저장합니다. 실제 UPDATE라 `analysis_runs` CHECK 42개를 DB가 직접 판정합니다. 모두 롤백됩니다. 정답 실행이 없는 DB(빈 DB·데모 시드)에서는 건너뜁니다.

## 3. T13 갈래

약자: V·AI·AO·VO = `VALIDATING`·`ANALYZING_IMPACT`·`ANALYZING_OPTIONS`·`VALIDATING_OUTPUT`.

| 갈래 | 일으키는 것 | 기대(상태 / 단계 / 코드 / 시도 / 소진) | 마지막 노드 | 호출 수 |
| --- | --- | --- | --- | --- |
| R00 | 없음 | READY_FOR_REVIEW | `validate` | 14 |
| R01 | T5 시도별 시간 제한 초과 3회 | FAILED / AI / `TOOL_TIMEOUT_EXHAUSTED` / 3 / true (**정답 RT-1**) | `inventory_impact:failed` | T5 3, 그 뒤 0 |
| R02 | T5 시간 초과 1회 + 일시 오류 1회 뒤 성공 | READY_FOR_REVIEW | `validate` | T5 3, 전체 16 |
| R03 | T2가 부를 때마다 2번 실패 뒤 성공(두 단계에서 부름) | READY_FOR_REVIEW | `validate` | T2 6, 전체 18 |
| R04 | 일시 오류 3회: T1·T9·T11·T12 | FAILED / AI·AO·VO·VO / `TOOL_TEMPORARY_ERROR_EXHAUSTED` / 3 / true | `<노드>:failed` | 그 도구 3, 그 뒤 0 |
| R05 | 권한 오류: T1·T7·T11·T12 | FAILED / 그 단계 / `PERMISSION_DENIED` / 1 / false | `<노드>:failed` | 그 도구 1 |
| R06 | T1 일시 오류 1회 뒤 권한 오류 | FAILED / AI / `PERMISSION_DENIED` / 2 / false | `coordinator:failed` | T1 2 |
| R07 | T10이 필수 자료 오류 | FAILED / AO / `UNEXPECTED_TOOL_ERROR` / 1 / false, 고정 문구(그 코드는 서버 로그 경고에) | `response_options:failed` | T10 1, T11 0 |
| R08 | T6 분류 안 된 예외 | FAILED / AI / `UNEXPECTED_TOOL_ERROR` / 1 / false | `inventory_impact:failed` | T6 1 |
| R09 | 실제 설명 함수 + 엔진 결함 3개(엔진 필요) | FAILED / VO / `EXPLANATION_GENERATION_FAILED` / 3 / true (**정답 RT-2**) | `explain` | 모델 3, T12 0 |
| R10 | 엔진 입력 보류: `REQUIRED_DATA_INVALID`(**정답 E9 v1**)·`MISSING_REQUIRED_DATA` | ON_HOLD / V / 그 코드 / 1 / false | `prepare` | 도구 0 |
| R11 | 도구 밖 예외: 설명 함수·엔진 입력 만들기 | FAILED / VO·V / `UNEXPECTED_TOOL_ERROR` / 1 / false, 고정 문구 | `<노드>:failed` | — |
| R12 | (T20 비교용) 재고 영향 뒤 STALE 확인 | STALE | `coordinator:stale_stop` | 8 |

- **R03이 보는 것:** 재시도 한도는 실행 전체가 아니라 도구 호출마다 따로입니다(agentflow 7.1 "마지막 도구의 최종 시도 횟수").
- **R07:** 보류는 분석 준비에서만 냅니다. 도구가 필수 자료 오류를 내면 코드 오류(FAILED)입니다.
- **R09:** 실패 문구 끝의 시도 기록, 모델·프롬프트 판이 정답 RT-2 행과 같습니다. 계산 결과(재고 영향·대응안·순위·판정 서명)는 상태에 남고 통과한 설명은 없습니다. 같은 연결에서 결함이 없으면 실제 T12까지 거쳐 READY로 끝나는 대조 시험도 있습니다.
- **공통 확인:** 실패 뒤 설명 함수를 부르지 않음, 실패 요약의 저장 열에서 결과 분류·의존 범위가 NULL, 문구에 예외 내용(접속 문자열·주문 코드)이 없음.
- **리드타임 `BUSINESS_DAY`(이슈 #61):** 분석 준비에서 `REQUIRED_DATA_INVALID` 보류로 정해졌으므로 그래프 모양은 R10과 같습니다. `BUSINESS_DAY` 행에서 그 보류가 나오는지는 입력 검증 쪽 시험이 봅니다.

### 3.1 저장

| 시험 | 내용 | 기대 |
| --- | --- | --- |
| RT-1 | R01 요약을 `AN-260110-001` 자리에 저장 | 저장 뒤 행 = 정답 행(문구만 서버 문구), 결과 표 행 0 |
| 보류 | R10 요약을 `AN-260102-002` 자리에 저장 | 저장 뒤 행 = 정답 행 전부(문구 포함) |
| RT-2 | R09 요약 + 그래프 상태의 모델·프롬프트 판 + 계산 결과를 `AN-260107-001` 자리에 저장(엔진 필요) | 저장 뒤 행 = 정답 행(모델 판·설명 검증 상태·판정 서명·결과 행 포함). 문구 = 서버 문구 + 정답 시도 기록 |
| 그 밖 14건 | R04·R05·R06·R07·R08·R10(`MISSING_REQUIRED_DATA`)·R11 요약을 되돌린 실행에 저장 | 행 = `FailureSummary.columns()`, 결과 표 행 0, 끝난 시각 있음 |

- **RT-2의 계산 결과 출처:** 정답 실행의 결과 행에서 되돌린 것입니다(`test_result_save.py`의 `_result_from_db`). 실패 요약·모델 판은 그래프가 만든 것입니다. 그래프 상태를 계산 결과로 바꾸는 일은 실행기(자동 시작, 이슈 #51) 몫이라 여기서 보지 않습니다.
- **이슈 #51 뒤 할 일:** "그래프 → 실행기 → 저장" 실제 경로 시험 1건을 더합니다.

## 4. T20 — 추적 장애

기준은 **추적 없이 돌린 결과**입니다. 비교 항목은 그래프 결과 전체, 도구별 호출 수, 설명·모델 호출 수, 실패 요약의 저장 열입니다.

| 장애 | 만드는 법 | 장애가 실제로 일어났는지 |
| --- | --- | --- |
| F0 | 정상 추적기 | `TOOL_ATTEMPT` 사건 수 = 도구가 센 시도 수, 처음 `RUN_START`·끝 `RUN_END` |
| F1 | 쓰기 예외 | `sink_failures` = 사건 수 |
| F2 | 쓰기 지연(사건마다 0.1초) | 걸린 시간 < 사건 수 × 0.1초 ÷ 2, 닫기를 기다리지 않아도 분석은 끝나 있음 |
| F3 | 추적기 `emit`이 예외 | 사건 0개, 콜백 경고·도구 시도 수만큼의 기록 실패 경고 |
| F4 | `tool_attempt`·`explanation_attempts`가 예외 | 도구 시도 수만큼의 기록 실패 경고, 설명 시도 기록 실패 경고 |
| F5 | 콜백 `on_chain_*`가 예외 | 실행·노드 사건 없음, LangChain이 예외를 삼킨 경고 |
| F6 | 내부 로그 기준 경로가 폴더가 아니라 파일 | `sink_failures` = 사건 수 |

- **행렬:** 갈래 9개(R00·R01·R02·R05-T7·R06·R09·R10·R11-설명·R12) × 장애 F0·F1·F3·F4·F5·F6 = 52건입니다. 이미 있는 R00×F1과, 장애가 날 곳이 없는 R10×F4는 뺐습니다. F2는 재시도 갈래 2개(R01·R02)에서 따로 봅니다.
- **장애를 삼키는 곳:** 추적기 메서드의 예외는 추적기가 아니라 부르는 쪽(`retry._record`·`TraceCallbackHandler`·`explanation._trace`)이 먼저 잡습니다. 그래서 F3·F4에서는 `sink_failures`가 늘지 않고 경고 로그가 남습니다.
- **F6:** 폴더를 읽기 전용으로 바꾸는 방법은 Windows에서 쓰기를 막지 못해, 기준 경로를 파일로 줍니다(폴더를 만들 수 없음).
- **비교가 값을 보는지:** 실패 요약의 시도 횟수 하나만 바꾼 사본이 "다름"으로 나오는 시험이 있습니다.

### 4.1 실행 ID 하나로 이어짐과 금지 항목

| 시험 | 확인 |
| --- | --- |
| R01 | `RUN_START` → 노드·갈림길 → T5 시도 1·2 `RETRY`·3 `ERROR`(`TOOL_TIMEOUT_EXHAUSTED`) → `RUN_END(FAILED, ANALYZING_IMPACT)`. 오류 사건(`NODE_ERROR`·`RUN_ERROR`)이 아니라 노드 끝으로 남음. 모든 사건의 실행·고객사·추적 ID가 같고 순번이 이어짐 |
| R02 | T5 시도 1 `RETRY`(시간 초과)·2 `RETRY`(일시 오류)·3 `OK` → `RUN_END(READY_FOR_REVIEW)` |
| R09 | 도구 시도 뒤에 `EXPLANATION_ATTEMPT` 3개. 추적의 시도 기록을 이으면 정답 RT-2 문구와 같음. 최종 확인(T12) 시도 없음 |
| 금지 항목 5갈래 | 예외 문구에 접속 문자열·주문 코드가 있어도 기록에는 없음 |
| 금지 항목 R09 | canary·지연 선적 코드가 없고, 설명 시도 사건은 사유 코드·모델·프롬프트 판만 가짐 |

## 5. 시험이 실패를 잡는지 (변이 시험)

운영 코드를 한 곳씩 일부러 바꿔 새 시험 121건을 돌리고, 끝난 뒤 원래대로 되돌렸습니다.

| 변이 | 바꾼 곳 | 실패한 새 시험 |
| --- | --- | --- |
| M1 재시도 한도 3 → 4 | `retry.MAX_ATTEMPTS` | 14건(재시도 갈래·저장·추적) |
| M2 권한 오류를 재시도 | `PermissionDeniedError.retryable` | 8건(R05·R06 호출 수·시도 횟수) |
| M3 시간 초과 코드를 일시 오류 코드로 | `retry._classify` | 5건(RT-1 기대값·저장·추적 시도 기록) |
| M4 시도별 시간 제한을 넘기지 않음 | `call_tool`의 `timeout_seconds` | 5건(R01, 1초 안에 끝나지 않음) |
| M5 도구의 필수 자료 오류를 보류로 둠 | `call_tool`의 보류 → 코드 오류 | 3건(R07) |
| M6 하위 에이전트가 실패해도 조정 에이전트로 | `route_to_coordinator` | 6건(멈춘 뒤 도구 호출 수) |
| M7 마지막 노드의 `:failed` 표시 삭제 | `with_failure_stop` | 15건 |
| M8 저장 열에서 `completed_status` 빠짐 | `FailureSummary.columns()` | 34건(저장 시험은 DB의 ck23이 UPDATE를 거부) |
| M9 도구 시도 기록 예외를 잡지 않음 | `retry._record` | 16건(F3·F4) |
| M10 설명 시도 기록 예외를 잡지 않음 | `explanation._trace` | 1건(R09 × F4) |
| M11 콜백 예외를 LangChain이 올리게 함 | `TraceCallbackHandler.raise_error = True` | 9건(F5 전부) |
| M12 쓰기 예외를 잡지 않음 | `RunTracer._write` | 17건(F1·F6) |
| (참고) 콜백 안의 `try`만 삭제 | `on_chain_start` | 0건. 핸들러가 `raise_error = False`라 LangChain이 한 번 더 삼킵니다(이중 방어). 한 곳만 지워서는 결과가 바뀌지 않는 것이 맞는 동작입니다 |

- 원래대로 되돌린 뒤 새 시험 121건 통과, `git status`로 `app/` 변경 없음을 확인했습니다.

## 6. 작업 중 실패와 고친 것

- **변이를 넣지 못한 첫 실행(내 실수):** 변이 스크립트가 운영 코드에서 바꿀 곳을 LF 줄바꿈으로 찾았는데 운영 코드 파일은 CRLF라, 첫 실행에서 M2~M12가 "대상 0곳"으로 끝났습니다(M1만 한 줄이라 들어감). 원본 파일은 바뀌지 않았습니다. 줄바꿈을 맞춰 다시 돌렸습니다.
- **"장애가 실제로 일어났는가"를 다르게 예상함:** 처음에는 추적기 메서드가 예외를 내면 `sink_failures`가 늘 것으로 봤습니다. 실험해 보니 부르는 쪽(`retry._record`·콜백·`explanation._trace`)이 먼저 잡아 0이었습니다. 그래서 F3·F4는 경고 로그 수로 확인합니다.
- **실제 설명 갈래가 비교 노드에서 멈춤:** 기본 가짜 T10·T11은 가짜 판정 서명에 맞춰져 있어 GS-05 엔진 판정 서명과 달랐습니다. 엔진 판정 서명에서 만든 T10·T11 가짜를 따로 두었습니다.
- **시간 초과 호출 수:** 취소된 시도가 늦게 세질까 걱정했지만, 도구에 들어올 때 세면 정확히 3이었습니다. 정확한 값으로 비교합니다.
- **내부 로그 쓰기 불가 만들기:** 폴더를 읽기 전용으로 바꾸는 방법은 Windows에서 쓰기를 막지 못했습니다. 기준 경로를 파일로 주는 방법으로 바꿨습니다(어느 OS에서나 폴더를 만들 수 없음).

## 7. 아직 안 한 것

- **실제 경로 저장:** 그래프 결과를 저장으로 잇는 실행기(이슈 #51)가 생기면 한 건을 더합니다. 그래프 밖으로 나간 예외(연결 실수)의 마지막 잡기도 그쪽 시험입니다.
- **실제 도구 묶음:** 이 시험은 가짜 도구 묶음에서 돕니다. 실제 도구 등록표가 생기면 같은 갈래 표를 그 묶음으로도 돌릴 수 있습니다.
- **CI:** DB와 엔진이 없는 환경에서는 저장 17건과 실제 설명 갈래가 건너뜁니다.
