# 분석 결과 저장 (정상 완료·실패·보류)

에이전트가 돌려준 분석 결과를 업무 DB에 저장하는 서비스입니다. 에이전트는 DB에 쓰지 않고(agentflow 5.5), 서비스가 이 함수로 저장합니다.

- 대상: `app/modules/delay_analysis/application/result_save/result_rows.py`(행 만들기, 순수 함수), `result_save_service.py`(잠금·상태 확인·저장)
- 시험: `tests/modules/delay_analysis/test_result_save.py`
- 범위: 정상 완료(`READY_FOR_REVIEW`·`NO_RISK`, 1~4절)와 실패·보류(`FAILED`·`ON_HOLD`, 5절). `NO_DELAY`와 자동 시작(백그라운드)은 범위 밖입니다(7절).

## 1. 부르는 법

```python
outcome = await save_analysis_result(
    db, company_id=..., analysis_run_id=..., result=AnalysisResult(...)
)
# outcome.saved: 저장했는가, outcome.status: 저장했으면 새 상태, 아니면 그때 상태(예: STALE)
```

- 커밋은 호출하는 쪽이 합니다. 에이전트 분석은 이 트랜잭션 밖에서 끝나 있어야 합니다(serviceflow 5.2 "긴 작업은 잠그지 않는다").
- `AnalysisResult` = 도구 출력 T6(`inventory_impact`)·T7(`at_risk_orders`)·T10(`option_outcomes`)·T11(`option_ranking`) + 의존 범위·판정 서명(엔진 `pipeline.analyze`) + 실행 기준값(`RunBasis`) + 검증을 통과한 설명(있으면).
- `RunBasis`는 `run_basis_from_tools(T1, T2, T4, T5, T8, T6의 SKU 코드)`로 만듭니다.

## 2. 순서와 STALE

1. 행을 먼저 만듭니다. 도구 출력끼리 맞지 않으면 `ResultSaveInputError`이고, 잠금도 잡지 않습니다.
2. 실행 잠금(L2, `lock_analysis_run`)을 잡고 최신 상태를 다시 읽습니다. 없거나 다른 고객사 실행이면 `AnalysisRunNotFoundError`입니다.
3. 상태가 진행 중(`DETECTED`~`VALIDATING_OUTPUT`)이 아니면 **아무것도 쓰지 않고** `saved=False`를 돌려줍니다. 분석하는 동안 수집이 실행을 `STALE`로 바꿨을 때입니다. STALE을 덮어쓰지 않고, 결과 행만 남는 반쪽 상태도 만들지 않습니다(티켓 "완료 처리는 진행 중일 때만"). DDL `analysis_runs_ck25`는 STALE에 완료 상태가 비어 있는 것을 허용합니다.
4. 결과 표 4개를 넣고 `analysis_runs`를 바꿉니다. 끝난 시각은 DB 시각(`clock_timestamp()`)입니다. 부르는 쪽이 `now`를 넘기면(가상 시각 데모, 이슈 #50) 끝난 시각 = `max(now, 실행 시작 + 1초)`이고, 재판정의 STALE 감지 시각도 이 값입니다(실행 생성과 같은 값을 넘겨도 소요 0초가 아님).
5. **같은 트랜잭션에서 다시 판정합니다(serviceflow 5.2 처리 단계 5, Q-13a).** 확정된 의존 범위로 입력 순번 이후 변경 전체를 봅니다(`StaleJudgmentService.recheck_run`). 분석하는 동안 진행 중 실행은 의존 범위가 없어 지연 선적 SKU로만 판정되었고, 수집 쪽 판정은 그 수집의 변경만 보기 때문입니다. 걸리면 저장한 결과는 그대로 두고 `STALE`로 기록하며, 반환값 `status`가 `STALE`입니다. STALE 시각은 끝난 시각과 같은 DB 시각입니다.
   - 2차(판정 서명 재계산)는 `recalculator` 인자로 넣습니다. 그래프 결과 저장(`save_graph_outcome`)은 `recalculator_or_none`을 넘겨, 서명이 바뀐 경우만 STALE입니다(이슈 #75, `docs/analysis/stale-signature-recalculation.md`). 엔진 경로가 없거나 넣지 않으면 의존 범위에 걸린 변경이 있을 때 STALE로 봅니다(놓치지 않는 쪽).
   - 종료된 에피소드·최신이 아닌 실행은 재판정하지 않습니다(동결, `recheck_run` 규칙).

## 3. 열 규칙 (엔진 `storage.py` `rows_for`와 같음)

| 표 | 행 | 도구 출력에 없어 여기서 정하는 값 |
| --- | --- | --- |
| `analysis_runs` | 실행 | `status = completed_status`(T6), `result_class`, `status_reason`(NO_RISK: `WARNING_NO_RISK`면 `NO_DELAY_INCREMENT; PRE_EXISTING_SHORTAGE`, `PURE_NO_RISK`면 `NO_DELAY_INCREMENT; NONE`, 엔진 하네스 `run_s1_s5.py` 167행), 의존 범위·판정 서명·SHA-256. 설명이 있으면 모델·프롬프트 버전·최종 설명·`PASSED`·생성 시각 |
| `inventory_impact_results` | T6 항목 | 도착 창고, `CALCULATED`, `ORDER_FIRST_APPROX_V1`, 판매 기준(T4, 일수는 운영 기준 값), 안전 재고 일수(T8), 계획·예상 입고일(T1), 행사 이름이 없으면 NULL, **Q11 세 값(4절)** |
| `at_risk_orders` | T7 활성 상세 | 배송일 → `T23:59:59Z`, `risk_reason` = `"{귀속} 예상 배송 {E 날짜} > 약속 {약속 날짜}"`(111행) |
| `response_options` | T10 대응안 | T11 순위·추천, `reason` = `reason_code`(118행 "FEASIBLE이면 NULL"과 같음. `response_options_ck5`가 FEASIBLE ⇔ 사유 코드 없음을 강제) |
| `option_order_outcomes` | T10 `outcomes[]` | `order_line_id` → 같은 저장의 위험 주문 행 ID |

- 새 행 ID는 UUIDv7입니다(`app/core/orm.py` 테이블 식별자 기본값·수집 적재와 같은 규칙, #38 리뷰). 엔진은 overlay를 다시 만들 때 같은 ID가 나오도록 `uuid5`를 씁니다. 서버 저장과 목적이 달라 맞추지 않습니다.
- **설명:** `READY_FOR_REVIEW`는 검증을 통과한 설명(`Explanation`)이 있어야 하고, `NO_RISK`에는 설명이 없습니다. 엔진은 READY만 설명을 만듭니다. 설명 없이 READY로 저장하면 최종 선택 재확인(FAILED만 거름)을 통과해 설명 없는 실행이 선택될 수 있어 막습니다(#38 리뷰). 설명이 계속 실패하면 실패 저장을 씁니다.

## 4. 계약 Q11 — T6 출력에 없는 저장 열

계약 문서(`docs/agent-flow/tool-contracts.md`) 9절 Q11은 "결과 저장에서 더한다"였습니다. T6 모델을 바꾸지 않고, 같은 실행의 조회 도구 출력에서 엔진 정의 그대로 계산합니다(`run_basis_from_tools`).

| 열 | 계산 | 엔진 근거 |
| --- | --- | --- |
| `available_quantity` | T2 도착 창고 `on_hand − reserved`(행이 없으면 0) | `storage.py` 88행 |
| `committed_open_quantity` | T5 중 **도착 창고** 주문의 **활성** 상세 주문 수량 합 | `core.forecast_sku` 25행, `core.to_alloc_order_lines` 54행 |
| `average_daily_sales` | T4 도착 창고 채널별 수요 합 ÷ 28 → float `round(·, 6)` → `repr` | `storage.py` 95행, `store.py` 133·134행(채널별 합 = 전체) |

- 계약 문서 9절 Q11 줄에 이 처리를 적었습니다(#33 머지 뒤).

## 5. 실패·보류 저장 (`save_analysis_failure`)

```python
outcome = await save_analysis_failure(
    db, company_id=..., analysis_run_id=..., summary=FailureSummary(...)
)
# 설명 실패만: computed=AnalysisResult(...), explanation_model=ExplanationModel(model, prompt)
```

- 실행 열 값은 실패 요약(`FailureSummary.columns()`, `agent/retry.py`)이 DDL 규칙(ck10~14·ck23·ck26·ck36)대로 만듭니다. 잠금·진행 중 확인·STALE이면 안 씀은 2절과 같습니다.
- **입력 검증 보류도 이 함수로 저장합니다.** 입력 검증은 판정만 하고 `hold_summary(코드, 메시지)`를 넘깁니다(사용자 결정 11-05, 같은 저장 규칙을 한 곳에 둠).

| 경우 | 결과 행 | 판정 서명 | 그 밖 (엔진 overlay와 같음) |
| --- | --- | --- | --- |
| 보류 `ON_HOLD`(예: `REQUIRED_DATA_INVALID`) | 없음 | 없음 | — |
| 도구 실패(예: `TOOL_TIMEOUT_EXHAUSTED`) | 없음 | 없음 | — |
| 설명 실패 `EXPLANATION_GENERATION_FAILED` | **계산 결과(`computed`) 저장** | **남김** | 의존 범위·결과 분류 비움(ck36·ck21), 검증 `FAILED`·최종 설명 없음(ck26), 설명을 만들려 한 모델·프롬프트 버전 남김 |

- 입력 거부(`ResultSaveInputError`): 설명 실패인데 계산 결과나 모델 버전이 없음, 계산 결과가 READY가 아니거나 설명이 붙어 있음(엔진은 READY만 설명을 만듦, `run_s1_s5.py` `analyze_run`), 보류·도구 실패인데 결과 행을 넘김.

- 실패·보류는 STALE 대상이 아니라(`stale_service` `STALE_TARGET_STATUSES`) 저장 뒤 재판정하지 않습니다. 잠금·진행 중 확인·STALE이면 안 씀은 정상 완료와 같은 코드입니다.
- **끝난 시각(`now`, 이슈 #50 후속):** 정상 완료 저장(2절 4번)과 같은 규칙입니다. 넘기지 않으면 DB 시각, 넘기면 `max(now, 실행 시작 + 1초)`입니다. 시각을 정하는 것은 부르는 쪽(자동 시작)이고, 이 함수는 가상 시각 설정을 보지 않습니다(이슈 #50 원칙). 같은 실행의 정상 저장·실패 저장·설명 생성은 같은 기준을 써야 합니다(가상 시각이면 실행 시작 시각을 넘기는 것이 가장 단순).

## 6. 시험

| 시험 | 내용 |
| --- | --- |
| Q11 계산 | 도착 창고만, 활성 상세만, 채널 합, 재고 행 없으면 0, 소수 6자리 |
| 입력 거부 | READY인데 대응안 없음, NO_RISK인데 순위 있음, Q11 값이 없는 SKU, 의존 범위·판정 서명 버전 |
| overlay 왕복 | DB의 정상 완료 실행 17개(STALE 14·FINALIZED 2·NO_RISK 1)마다: 결과 행을 도구 출력으로 되돌림 → 결과 행을 지우고 실행을 진행 중으로 되돌림 → 저장 → 원래 행과 열 단위로 같음(ID 제외). 재고 영향 376·위험 주문 7·대응안 36·주문 결과 21. 모두 롤백 |
| STALE | 저장 직전 STALE이면 `saved=False`, 결과 행 0, 상태·완료 상태·끝난 시각 그대로 |
| 설명 | READY인데 설명 없음, NO_RISK인데 설명 있음 → 거부 |
| 완료 뒤 재판정 | 입력 순번 뒤 관련 변경으로 STALE이 된 최신 실행의 에피소드를 (시험 안에서) 다시 열고 저장 → 저장 직후 STALE, 완료 상태 READY 보존, 감지 순번이 입력 순번 뒤. 가짜 재계산기가 "서명 그대로"면 READY 유지 |
| 새 행 ID | 왕복 시험에서 넣은 행 ID가 모두 UUIDv7 |
| 실패·보류 왕복 | overlay의 ON_HOLD 1(E9 v1)·도구 시간 초과 FAILED 1·설명 실패 FAILED 1을 실패 요약으로 되돌려 다시 저장 → 실행 열 17개와 결과 행이 원래와 같음(설명 실패: 재고 영향 15·대응안 3) |
| 실패 저장 시각 | 실행 시작과 같은 `now` → 끝난 시각 = 시작 + 1초, 더 늦은 `now` → 그대로, 넘기지 않음 → 저장 전후 DB 시각 사이 |
| 설명 붙은 계산 결과 거부 | overlay 설명 실패 실행을 되돌린 계산 결과(다른 검사는 모두 통과)에 설명만 붙이면 거부·아무것도 안 씀, 설명을 빼면 저장됨. 이 분기를 끄면(`or False`) 시험이 실패함을 확인(#48 리뷰 후속) |
| 실패 입력 거부 | 5절 네 경우 |
| 실패 STALE | 저장 직전 STALE이면 보류도 쓰지 않음 |

- **음성(시험이 실패를 잡는지, 11-05):** 상태 확인을 빼면 STALE 시험이, 하회일을 기준선 값으로 바꾸거나 위험 사유 문구를 바꾸면 왕복 시험이, 비활성 상세도 세면 Q11 시험이 실패합니다. "FEASIBLE이면 `reason` NULL" 조건을 빼는 변경은 실패하지 않았고, 원인은 ck5(같은 결과)여서 조건을 지웠습니다.
- **#38 리뷰 반영 음성:** 재판정을 빼면 재판정 시험이, 설명 검사를 빼면 설명 시험이, 재계산기를 넘기지 않으면 "서명 그대로" 시험이, 새 ID를 `uuid4`로 되돌리면 왕복 시험이 실패합니다.
- **실패·보류 음성:** 설명 실패의 판정 서명이나 모델 이름을 빼면 실패 왕복이, 보류에 결과 행을 허용하거나 READY 검사를 끄면 입력 거부가, 공용 상태 확인을 빼면 두 STALE 시험이 실패합니다. "정상 완료 값과 실패 요약을 섞어도" 결과가 같은 경우가 있어(READY의 `status_reason`은 원래 NULL) READY·설명 없음 검사를 넣었습니다.
- **확인하지 못한 것:** 왕복 시험은 Q11 세 값을 DB 행에서 가져와 넣습니다. 실제 조회 도구(T2·T4·T5) 출력으로 같은 값이 나오는지는 조회 도구가 생긴 뒤 통합 시험으로 확인해야 합니다.
- 정상 완료 왕복은 설명 실패 `FAILED` 1건을 빼고, 그 1건은 실패·보류 왕복에서 봅니다.

## 7. 범위 밖과 다음 일

- `NO_DELAY`(판정 서명 있음, 결과 행 없음, 데이터 버전 기록): 입력 검증 작업. 입력 검증 `ON_HOLD`의 판정도 입력 검증 작업이고, 저장은 5절 함수를 씁니다.
- 자동 시작(감지 → 실행 생성 → 입력 검증 → 백그라운드 분석 → 이 저장): 노드 연결 작업 뒤.
- 데모 시드(분석 결과 행을 넣는 일)가 이 서비스를 쓰면 열 규칙이 한 곳에 모입니다.

## 8. 작업 중 실패와 원인

- **완료 처리의 재판정을 빠뜨림(#38 리뷰 critical):** 계획할 때 serviceflow 5.2 처리 단계를 1~3단계까지만 읽고 5단계(완료 처리 뒤 재판정, Q-13a)를 놓쳤습니다. develop에 `recheck_run`이 이미 있었습니다. 저장 흐름 2절 5번으로 넣었습니다.

- **시험 기대 숫자를 짐작으로 적음:** 처음에 왕복 행 수를 재고 영향 368·대응안 37로 적었는데, 시험을 돌리기 전에 SQL로 따로 세어 보니 376·36이었습니다. 전체 391·39에서 설명 실패 실행 1건의 행(재고 영향 15·대응안 3)을 빼야 하는데 짐작으로 적은 값이 틀렸습니다. 센 값으로 고쳤습니다.
- **잡히지 않은 음성 변경:** 위 6절. 같은 결과를 내는 변경이었습니다.
- **음성 변경이 적용되지 않은 채 "통과"로 읽힘:** ruff가 줄 모양을 바꿔 바꿀 문자열을 못 찾았는데 시험이 그대로 돌아 "통과"가 나왔습니다. 적용 여부를 출력으로 확인한 뒤 다시 했습니다.
- **시험 준비 순서:** 실패 실행을 진행 중으로 되돌릴 때 실패 요약 열보다 상태를 먼저 바꿔 ck10에 걸렸습니다. 실패 요약 열부터 비우도록 고쳤습니다(저장 코드 문제 아님).

## 9. 지연 해소 저장 (`save_no_delay`, 분석 자동 시작 GLOW-128)

```python
outcome = await save_no_delay(
    db,
    company_id=company_id,
    analysis_run_id=run_id,
    decision_signature=no_delay.decision_signature,  # judge_no_delay 결과(엔진 pipeline.no_delay)
    decision_signature_sha256=no_delay.decision_signature_sha256,
    now=None,  # 가상 시각이면 실행 시작 시각
)
```

| 항목 | 규칙 | 근거 |
| --- | --- | --- |
| 쓰는 열 | `status`·`completed_status` = `NO_DELAY`, 판정 서명·SHA-256, 끝난 시각 | DDL ck23·ck39~41, 확정 DB E8 v2 |
| 비우는 열 | 결과 분류·의존 범위·실패 요약 5열·설명 3열·STALE 4열·`status_reason`(쓰지 않음) | ck10·ck20~22·ck27·ck37, 확정 DB E8 v2 모두 NULL |
| 결과 행 | 없음(표 4개 모두) | 명세 4.5 "NO_DELAY 결과 행이 없다" |
| 재판정 | 하지 않음 | `NO_DELAY`는 STALE 대상이 아님(serviceflow 5.2, ck25) |
| 에피소드 | 보지 않음 | 회복 실행은 에피소드가 닫힌 뒤 분석된다(#63, 확정 DB E8 v2) |
| 입력 확인 | 잠그기 전에 판정 서명이 `decision_signature.v1`·`status NO_DELAY`·`options []`이고 SHA-256이 소문자 16진수 64자리인지. 아니면 `ResultSaveInputError` | ck39·ck41을 DB 오류가 아니라 입력 오류로 |
| 끝난 시각 | 다른 저장 함수와 같음(없으면 DB 시각, 있으면 max(now, 시작 + 1초)). 확정 DB는 소요 0초(하네스가 끝난 시각 = 시작) | 이슈 #39 규칙 3 |

시험(`tests/modules/delay_analysis/test_run_start_sql.py`): 확정 DB E8 v2를 진행 중으로 되돌려 같은 서명으로 저장하면 끝난 시각만 빼고 확정 행과 열 단위로 같습니다. 서명이 틀리면 저장 전에 거부되고, STALE이면 아무것도 쓰지 않습니다.
