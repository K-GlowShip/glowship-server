# 분석 결과 저장 (정상 완료)

에이전트가 돌려준 분석 결과를 업무 DB에 저장하는 서비스입니다. 에이전트는 DB에 쓰지 않고(agentflow 5.5), 서비스가 이 함수로 저장합니다.

- 대상: `app/modules/delay_analysis/application/result_save/result_rows.py`(행 만들기, 순수 함수), `result_save_service.py`(잠금·상태 확인·저장)
- 시험: `tests/modules/delay_analysis/test_result_save.py`
- 범위: 정상 완료(`READY_FOR_REVIEW`·`NO_RISK`). 실패·보류(`FAILED`·`ON_HOLD`)와 `NO_DELAY`, 자동 시작(백그라운드)은 이 문서 범위 밖입니다(6절).

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
4. 결과 표 4개를 넣고 `analysis_runs`를 바꿉니다. 끝난 시각은 DB 시각(`clock_timestamp()`)입니다.

## 3. 열 규칙 (엔진 `storage.py` `rows_for`와 같음)

| 표 | 행 | 도구 출력에 없어 여기서 정하는 값 |
| --- | --- | --- |
| `analysis_runs` | 실행 | `status = completed_status`(T6), `result_class`, `status_reason`(NO_RISK: `WARNING_NO_RISK`면 `NO_DELAY_INCREMENT; PRE_EXISTING_SHORTAGE`, `PURE_NO_RISK`면 `NO_DELAY_INCREMENT; NONE`, 엔진 하네스 `run_s1_s5.py` 167행), 의존 범위·판정 서명·SHA-256. 설명이 있으면 모델·프롬프트 버전·최종 설명·`PASSED`·생성 시각 |
| `inventory_impact_results` | T6 항목 | 도착 창고, `CALCULATED`, `ORDER_FIRST_APPROX_V1`, 판매 기준(T4, 일수는 운영 기준 값), 안전 재고 일수(T8), 계획·예상 입고일(T1), 행사 이름이 없으면 NULL, **Q11 세 값(4절)** |
| `at_risk_orders` | T7 활성 상세 | 배송일 → `T23:59:59Z`, `risk_reason` = `"{귀속} 예상 배송 {E 날짜} > 약속 {약속 날짜}"`(111행) |
| `response_options` | T10 대응안 | T11 순위·추천, `reason` = `reason_code`(118행 "FEASIBLE이면 NULL"과 같음. `response_options_ck5`가 FEASIBLE ⇔ 사유 코드 없음을 강제) |
| `option_order_outcomes` | T10 `outcomes[]` | `order_line_id` → 같은 저장의 위험 주문 행 ID |

- 새 행 ID는 `uuid4`입니다. 엔진은 overlay를 다시 만들 때 같은 ID가 나오도록 `uuid5`를 씁니다. 서버는 한 트랜잭션에서 한 번 저장하므로 고정 ID가 필요 없습니다.

## 4. 계약 Q11 — T6 출력에 없는 저장 열

계약 문서(`docs/agent-flow/tool-contracts.md`) 9절 Q11은 "결과 저장에서 더한다"였습니다. T6 모델을 바꾸지 않고, 같은 실행의 조회 도구 출력에서 엔진 정의 그대로 계산합니다(`run_basis_from_tools`).

| 열 | 계산 | 엔진 근거 |
| --- | --- | --- |
| `available_quantity` | T2 도착 창고 `on_hand − reserved`(행이 없으면 0) | `storage.py` 88행 |
| `committed_open_quantity` | T5 중 **도착 창고** 주문의 **활성** 상세 주문 수량 합 | `core.forecast_sku` 25행, `core.to_alloc_order_lines` 54행 |
| `average_daily_sales` | T4 도착 창고 채널별 수요 합 ÷ 28 → float `round(·, 6)` → `repr` | `storage.py` 95행, `store.py` 133·134행(채널별 합 = 전체) |

- 계약 문서 Q11 줄은 이 PR에서 고치지 않았습니다. 같은 파일을 #33이 고치고 있어서, #33 머지 뒤 "결과 저장에서 계산(이 문서 4절)"으로 바꿉니다.

## 5. 시험

| 시험 | 내용 |
| --- | --- |
| Q11 계산 | 도착 창고만, 활성 상세만, 채널 합, 재고 행 없으면 0, 소수 6자리 |
| 입력 거부 | READY인데 대응안 없음, NO_RISK인데 순위 있음, Q11 값이 없는 SKU, 의존 범위·판정 서명 버전 |
| overlay 왕복 | DB의 정상 완료 실행 17개(STALE 14·FINALIZED 2·NO_RISK 1)마다: 결과 행을 도구 출력으로 되돌림 → 결과 행을 지우고 실행을 진행 중으로 되돌림 → 저장 → 원래 행과 열 단위로 같음(ID 제외). 재고 영향 376·위험 주문 7·대응안 36·주문 결과 21. 모두 롤백 |
| STALE | 저장 직전 STALE이면 `saved=False`, 결과 행 0, 상태·완료 상태·끝난 시각 그대로 |

- **음성(시험이 실패를 잡는지, 11-05):** 상태 확인을 빼면 STALE 시험이, 하회일을 기준선 값으로 바꾸거나 위험 사유 문구를 바꾸면 왕복 시험이, 비활성 상세도 세면 Q11 시험이 실패합니다. "FEASIBLE이면 `reason` NULL" 조건을 빼는 변경은 실패하지 않았고, 원인은 ck5(같은 결과)여서 조건을 지웠습니다.
- **확인하지 못한 것:** 왕복 시험은 Q11 세 값을 DB 행에서 가져와 넣습니다. 실제 조회 도구(T2·T4·T5) 출력으로 같은 값이 나오는지는 조회 도구가 생긴 뒤 통합 시험으로 확인해야 합니다.
- 설명 실패 `FAILED` 1건(결과 행 있음)은 상태 저장이 실패 요약 쪽 일이라 왕복 시험에서 뺐습니다.

## 6. 범위 밖과 다음 일

- 실패·보류 저장: 실패 요약(`FailureSummary`, 재시도 PR) 머지 뒤. 설명 실패 `FAILED`는 결과 행은 이 함수와 같고 실행 열만 다릅니다.
- `NO_DELAY`·입력 검증 `ON_HOLD`: 입력 검증 작업.
- 자동 시작(감지 → 실행 생성 → 입력 검증 → 백그라운드 분석 → 이 저장): 노드 연결 작업 뒤.
- 데모 시드(분석 결과 행을 넣는 일)가 이 서비스를 쓰면 열 규칙이 한 곳에 모입니다.

## 7. 작업 중 실패와 원인

- **시험 기대 숫자를 짐작으로 적음:** 처음에 왕복 행 수를 재고 영향 368·대응안 37로 적었는데, 시험을 돌리기 전에 SQL로 따로 세어 보니 376·36이었습니다. 전체 391·39에서 설명 실패 실행 1건의 행(재고 영향 15·대응안 3)을 빼야 하는데 짐작으로 적은 값이 틀렸습니다. 센 값으로 고쳤습니다.
- **잡히지 않은 음성 변경:** 위 5절. 같은 결과를 내는 변경이었습니다.
