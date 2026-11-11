# 도구 재시도와 실행 실패 요약

- 코드: `app/modules/delay_analysis/application/agent/retry.py`
- 시험: `tests/modules/delay_analysis/test_agent_retry.py`
- 근거: agentflow 7절·7.1절, API 명세 v6.2 4.5(분석 실행 상세의 실패 요약), DDL `analysis_runs` CHECK(특히 ck6·ck7·ck10~14·ck21·ck23·ck26·ck36·ck37·ck40), 정답 overlay(`analysis_runs` E3 v2·E9 v1·E2 v1), 엔진 `explain/generator.py`·`bundle_verify.py`
- 아직 하지 않은 것: 그래프 노드에 끼우기, 실패 요약 저장(실행 종료 기록 서비스), 추적 연결. 이 문서의 함수는 그 작업들이 부릅니다.

## 1. 무엇을 하나
| 기능 | 함수 | 내용 |
| --- | --- | --- |
| 도구 부르기 | `call_with_retry(tool_name, step, call, …)` | 최초 1회 + 재시도 최대 2회. 시도마다 시간 제한(`timeout_seconds`)과 대기(`backoff_seconds`, 기본 0.2초·0.5초). 시도마다 `on_attempt`로 기록을 넘깁니다(추적 연결용, 여기서 난 오류는 분석을 멈추지 않음) |
| 멈출 때 | `ToolFailure(summary)` | 노드는 이것을 받아 실행을 끝냅니다. `summary`는 `FailureSummary` |
| 실패 요약 | `FailureSummary` | 상태 + 5컬럼(`failed_step`·`failure_code`·`attempt_count`·`retry_exhausted`·`failure_message`). 만들 때 DDL 규칙을 먼저 확인합니다 |
| 저장할 값 | `FailureSummary.columns()` | 실행을 실패·보류로 끝낼 때 `analysis_runs`에서 바꿔야 하는 열을 **모두** 돌려줍니다. 서비스가 그대로 넣습니다: 상태와 5컬럼, `completed_status = status`(ck23), `result_class = NULL`(ck21), `dependency_scope = NULL`(ck36). 설명 실패면 `explanation_validation_status = FAILED`, `final_explanation_json = NULL`, `explanation_generated_at = NULL`도 넣습니다(ck26·ck7). `decision_signature`는 바꾸지 않습니다(ck37·ck40이 막지 않음) |
| 보류 | `hold_summary(code, message, attempt_count=1)` | 필수 자료 부족·검증 오류. 문구는 판정한 쪽 것을 그대로 씁니다(예: 엔진 입력 보류 `EngineHold`의 메시지). 시도 횟수는 마지막 도구의 실제 횟수입니다 |
| 설명 실패 | `explanation_failure(attempt_detail)` | 엔진 생성기가 3회 모두 실패했을 때. 엔진의 시도 기록 문자열을 문구 뒤에 붙입니다 |

## 2. 오류별 처리
| 오류 | 어떻게 알아보나 | 재시도 | 멈출 때 상태 / 단계 / 코드 / 시도 / 소진 | 근거 |
| --- | --- | --- | --- | --- |
| 시간 초과 | `TimeoutError`(시도 시간 제한 포함) | 함 | FAILED / 그 단계 / `TOOL_TIMEOUT_EXHAUSTED` / 3 / true | API 명세 4.5 "FAILED(도구 실패, 예: `TOOL_TIMEOUT_EXHAUSTED`)", 정답 E3 v2, 엔진 `bundle_verify` rt1 |
| 그 밖의 일시 오류 | 도구가 `TransientToolError` | 함 | FAILED / 그 단계 / `TOOL_TEMPORARY_ERROR_EXHAUSTED` / 3 / true | **제안**(아래 5절) |
| 권한 오류 | `PermissionDeniedError` | 안 함 | FAILED / 그 단계 / `PERMISSION_DENIED` / 그 시도 / false | agentflow 7.1 |
| 필수 자료 오류 | `RequiredDataError` | 안 함 | ON_HOLD / `VALIDATING` / `REQUIRED_DATA_INVALID`·`MISSING_REQUIRED_DATA` / 그 시도(첫 시도면 1) / false. **그래프 도구에서 나면 `call_tool`이 FAILED / 그 단계 / `UNEXPECTED_TOOL_ERROR` / 그 시도 / false로 바꿉니다**(결정 1: 보류는 엔진 입력 단계에서만, `agent-tool-interface.md` 4절) | API 명세 4.5, 정답 E9 v1(첫 시도 보류라 1), agentflow 7.1 |
| 분류 안 된 예외 | 그 밖의 `Exception` | 안 함 | FAILED / 그 단계 / `UNEXPECTED_TOOL_ERROR` / 그 시도 / false | **제안**(아래 5절) |
| 설명 3회 실패 | 엔진 생성기 결과 | 엔진이 함 | FAILED / `VALIDATING_OUTPUT` / `EXPLANATION_GENERATION_FAILED` / 3 / true | DDL ck26, 정답 E2 v1 |

- 일시 오류 뒤에 재시도하지 않는 오류가 나면, 그 시도 번호를 남기고 소진은 false입니다(예: 시간 초과 1회 → 권한 오류 = 시도 2, 시간 초과 1회 → 필수 자료 오류 = ON_HOLD 시도 2).
- `backoff_seconds`가 모자라면 마지막 값을 쓰고, 비어 있으면 기다리지 않고 재시도합니다.
- 안쪽 도구 호출이 이미 멈춰 `ToolFailure`가 올라오면 그대로 올립니다(다시 분류하지 않음).
- `asyncio.CancelledError`(취소)는 잡지 않습니다.

## 3. DDL 규칙과 같은지
`FailureSummary`가 만들 때 확인하는 것과 DB 제약이 같습니다.
- **확인 방법:** 엔진 결과 값(결과 분류·의존 범위·판정 서명·최종 설명)이 찬 실제 행(FINALIZED·NO_RISK)에 `columns()`를 덮어씁니다. 그 행을 **모델의 CHECK 42개 전부**로 실제 DB에서 SELECT 판정합니다(읽기 전용, 저장하지 않음).
- **이 행을 쓰는 이유:** 진행 중 행에 엔진 값이 남아 있는 최악의 경우를 대신합니다. 그래서 끝낼 때 비워야 할 열을 빠뜨리면 시험이 잡습니다.

| 제약 | 규칙 | `FailureSummary` |
| --- | --- | --- |
| ck10 | `FAILED`·`ON_HOLD`일 때만 `failed_step`이 있음 | 상태가 둘이 아니면 `ValueError` |
| ck11 | 5컬럼이 모두 차거나 모두 빔 | 코드·문구가 비면 `ValueError` |
| ck12 | 단계는 `VALIDATING`·`ANALYZING_IMPACT`·`ANALYZING_OPTIONS`·`VALIDATING_OUTPUT` | `AnalysisStep`이 아니면 `TypeError` |
| ck13 | 시도 1~3 | 밖이면 `ValueError` |
| ck14 | 소진이면 시도 3 | 어긋나면 `ValueError` |
| ck26 | 설명 실패는 `FAILED`·`VALIDATING_OUTPUT`·3회·소진·설명 검증 `FAILED`·최종 설명 없음 | 앞 4개를 확인하고, 뒤 2개는 `columns()`가 채움 |
| ck23 | `FAILED`·`ON_HOLD` 등 결과 상태면 `completed_status = status` | `columns()`가 채움 |
| ck21 | `result_class`는 `completed_status`가 `READY_FOR_REVIEW`·`NO_RISK`일 때만 | `columns()`가 NULL |
| ck36 | `FAILED`·`ON_HOLD`는 `dependency_scope` NULL | `columns()`가 NULL |
| ck7 | 최종 설명 ⇔ 생성 시각 | 설명 실패면 `columns()`가 둘 다 NULL |
| (API 명세) | `ON_HOLD`는 `VALIDATING`·보류 코드 | 어긋나면 `ValueError` |

## 4. 결정한 것
| 결정 | 이유 |
| --- | --- |
| 도구 단위로 재시도(LangGraph 노드 재시도 기능은 쓰지 않음) | agentflow 7.1은 "마지막 도구의 최종 시도 횟수"를 남기라고 합니다. 노드 단위 재시도는 그 수를 남기지 않고, 성공한 앞 도구까지 다시 부릅니다 |
| 설명 재생성은 여기서 하지 않음 | 엔진 `explain/generator.py`가 최대 3회를 이미 합니다(`MAX_ATTEMPTS = 3`). 서버는 그 결과만 실패 요약으로 옮깁니다 |
| `TOOL_TIMEOUT_EXHAUSTED`를 씀 | agentflow 7.1 표의 예시 코드는 `UPSTREAM_TIMEOUT`이지만, API 명세·정답 데이터·엔진 검증기는 모두 `TOOL_TIMEOUT_EXHAUSTED`입니다. 정답 데이터를 따랐습니다 |
| `ON_HOLD`의 단계는 늘 `VALIDATING` | API 명세 4.5와 정답 E9 v1이 그렇습니다. 필수 자료 오류가 재고 영향 도구에서 나도 `VALIDATING`으로 남습니다 |
| 분류 안 된 예외는 재시도하지 않음 | 코드 오류일 수 있어서입니다. 같은 입력이면 같은 오류가 나므로 재시도해도 소용없습니다 |
| `failure_message` 문구는 한 곳에서 만듦 | 문서마다 형식이 다릅니다(5절). 바꿀 때 `retry.py`의 `_tool_message`·`TOOL_LABELS`(도구의 화면 이름)·`UNEXPECTED_ERROR_MESSAGE`만 고치면 됩니다 |
| 이번에 `state.py`는 고치지 않음 | 상태에 `FailureSummary`를 넣으면 `state.py` ↔ `retry.py`가 서로 import 합니다. LangGraph는 실행 때 상태의 타입 힌트를 읽으므로, 그래프에 끼우는 작업에서 함께 정합니다 |

## 5. 확인이 필요한 것
- **설계 문서에 없는 코드 2개(제안):** 시간 초과가 아닌 일시 오류 `TOOL_TEMPORARY_ERROR_EXHAUSTED`, 분류 안 된 예외 `UNEXPECTED_TOOL_ERROR`. DB는 `failure_code`를 자유 문자열로 받으므로 제약에는 걸리지 않습니다.
- **`failure_message` 형식:**
  - agentflow 7.1은 "Client에게 보여 줄 사유와 재실행 방법"(예: "판매량 조회가 시간 초과로 3번 모두 실패했습니다. 잠시 뒤 재분석을 요청하세요.")입니다.
  - 정답 overlay는 기술 기록(예: "도구 시간 초과 3회(RT-1 주입)")입니다.
  - 정답 검증기(`bundle_verify` rt1)는 문구를 비교하지 않습니다.
  - 그래서 도구 실패는 agentflow 형식, 보류는 판정한 쪽 문구 그대로, 설명 실패는 agentflow 형식 + 엔진 시도 기록으로 정했습니다.
  - **Client에게 내부 표기를 보이지 않음(이슈 #57 2번, 11-12):**
    - 도구 이름은 내부 함수 이름(`get_sales_history`) 대신 화면 이름 + 조사로 씁니다("판매량 조회가 …"). agentflow 7.1 예와 같은 문장이 됩니다. 설계 문서에는 T4 예만 있어 나머지 11개는 같은 꼴로 정했습니다(`TOOL_LABELS`). 표에 없는 이름은 "분석 도구"로 씁니다.
    - 예상하지 못한 오류(`UNEXPECTED_TOOL_ERROR`)는 원인과 상관없이 고정 문구 하나(`UNEXPECTED_ERROR_MESSAGE`, 도구 밖 예외 #68과 같은 문장)입니다. 대응안 대조 차이·최종 확인 오류 목록·도구 단계 보류의 원래 코드·등록 안 된 도구 이름은 `ToolFailure.detail`로 넘겨 서버 로그 경고 한 줄(`graph.with_failure_stop`, 실행 ID 포함)로만 남깁니다.
    - 설명 실패의 엔진 시도 기록 "(시도: …)"은 정답 저장 문구와 대조하는 시험이 있어 그대로 둡니다. 화면은 이 코드에 고정 문구를 씁니다(`docs/frontend-api.md` 실패 코드 표).
- **대기 시간(0.2초·0.5초):** 설계 문서에 값이 없습니다. NFR-001(30초 안에 결과)을 넘지 않게 짧게 잡았고, 호출하는 쪽이 바꿀 수 있습니다.

## 6. 시험
| 시험 | 확인하는 것 |
| --- | --- |
| 재시도 12건 | 시간 초과 3회 → 소진(정답 E3 v2와 같은 5개), 2회 실패 뒤 성공, 일시 오류 3회, 시도 시간 제한, 권한·필수 자료·분류 안 된 예외는 1회, 일시 → 권한(시도 2), 일시 → 필수 자료(ON_HOLD 시도 2), 빈 `backoff_seconds`, 안쪽 실패 그대로, 기록 훅 장애 무시 |
| 값 규칙 11건 | 설명 실패·보류의 `columns()` 열, 보류 코드가 data_sync와 같음, 틀린 값 8가지를 거부 |
| 실제 DB CHECK 4건 | 모델의 CHECK가 42개, 만드는 요약 8종이 FINALIZED·NO_RISK 실제 행 위에서 42개 모두 통과(기준 행 자체도 통과), 열을 빼거나 틀리게 하면 ck23·ck21·ck36·ck7·ck10~14·ck26이 각각 거부 |
| 통합 시험 T13(그래프 전체) | 연결된 그래프에서 단계별 재시도 수·도구 호출 수·실패 요약과, 그 요약을 저장한 실제 행까지: `docs/agent-flow/integration-tests-t13-t20.md` 3절 |

**시험이 실패를 잡는지(변이 시험):** 코드를 일부러 바꿔 시험이 실패하는지 확인했습니다. 끝난 뒤 원래대로 돌렸습니다.
- 최대 시도 3 → 4: 6건 실패
- ck14 검사 삭제: 1건 실패
- 안쪽 `ToolFailure` 그대로 올리기 삭제: 1건 실패
- (리뷰 반영 뒤) 리뷰 전 `columns()`로 되돌림: 5건 실패, 생성 시각 NULL 빼기: 2건, 보류 시도 1 고정: 1건, 빈 `backoff` 처리 빼기: 1건

## 7. 작업 중 실패와 고친 것
- **안쪽 실패를 다시 분류하던 문제:** 처음에는 `call_with_retry` 안의 도구가 또 `call_with_retry`를 써서 멈추면, 바깥에서 그 `ToolFailure`를 "분류 안 된 예외"로 다시 바꿨습니다. 시험을 쓰기 전 다시 읽다가 찾았고, 그대로 올리도록 고쳤습니다.
- **`state.py`에 필드 추가 취소:** 처음 계획은 그래프 상태에 `failure`를 넣는 것이었습니다. 순환 import 때문에 취소했습니다(4절).
- **ruff 지적 3건:**
  - 단계 타입 오류는 `TypeError`로 바꿨습니다(TRY004).
  - 쓰지 않는 `noqa`를 지웠습니다.
  - 기록 훅의 예외를 조용히 버리던 곳에 경고 로그를 남겼습니다(S110).
- **`columns()`가 그대로 저장되지 않던 문제(PR #28 리뷰, 내 실수):**
  - 처음에는 "그대로 넣는 값"이라고 적었습니다. 그런데 `completed_status`(ck23)가 빠졌고, `result_class`(ck21)·`dependency_scope`(ck36)를 비우지 않았습니다.
  - 시험이 ck10~14·ck26만 골라 보고, 빈 가상 행 위에서 판정했기 때문에 잡히지 않았습니다.
  - 이제 세 열(설명 실패면 `explanation_generated_at`도)을 넣습니다. 시험은 CHECK 42개 전부를 엔진 값이 찬 실제 행 위에서 판정합니다.
  - 같은 리뷰의 비차단 제안 두 가지도 고쳤습니다: 보류의 실제 시도 횟수, 빈 `backoff_seconds`.
