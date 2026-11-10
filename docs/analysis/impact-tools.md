# 재고 영향 계산 도구 T7·T6 (엔진 결과에서 옮기기)

- 대상: `app/modules/delay_analysis/application/agent/tools/impact_tools.py`(도구), `impact_schemas.py`(`expected_plan_simulation` 형식), 시험 `tests/modules/delay_analysis/test_impact_tools.py`
- 티켓: GLOW-126 (상위 GLOW-23, FR-010·BR-005)
- 계약: `docs/agent-flow/tool-contracts.md` 4절, 9절 Q3·Q10·Q11·Q12
- 앞 작업: 실행 하나의 엔진 입력과 조회 도구(`docs/analysis/engine-input.md`)

## 1. 방식
- 엔진은 `pipeline.analyze(snap)` **한 번**으로 예측 층(`fc`)·주문 층(`ol`)·영향 집합(`k_imp`·`k_watch`)·결과 분류를 함께 계산합니다(`pipeline.py` 64~79행). 판정 서명도 이 결과로 만듭니다.
- T7·T6은 실행마다 한 번 만든 그 결과(`ctx.engine.analysis`)를 계약 4절 출력 모델로 **옮기기만** 합니다. 다시 계산하지 않으므로 도구 출력·판정 서명·대응안이 같은 계산에서 나옵니다(조회 도구 T1~T8과 같은 방식).
- 노드가 넘기는 입력(`IdentifyAtRiskOrdersInput`·`CalculateInventoryImpactInput`)은 계산에 쓰지 않습니다. 이 실행의 엔진 입력과 맞는지만 확인합니다(선적·기준일, T6은 T7의 지연 유발 SKU도). 다르면 `ValueError`입니다(다른 실행이나 다른 순서로 부른 것, 재시도 없이 FAILED).
- NO_DELAY·보류 실행은 엔진 결과에 예측 층·주문 층이 없어 부르지 않습니다(부르면 멈춤).

## 2. 엔진 → 계약
| 계약 | 엔진 | 옮길 때 |
| --- | --- | --- |
| T7 `risks[]` | `ol["risks"]` | `order_code` 순, 줄은 `line_no` 순. `sales_channel` = `channel`, `expected_delivery_date`·`baseline_expected_delivery_date` = `delivery_E`·`delivery_P`, `expected_beyond_horizon` = `beyond` |
| T7 `ordered_at` | 스냅샷 도착 창고 열린 주문의 `ordered_at` | 엔진 `ol`은 접수 시각을 날짜(`ordered`)로 줄여 두어서, 계약의 시각은 스냅샷 원래 값에서 가져옵니다 |
| T7 `wait_cause` | `ol` 줄의 `wait_cause`(E 시뮬레이션 D일 출력) | 엔진은 배정기 v5 순회를 써서 `DIRECT_SKU_SHORTAGE`·`BUNDLED_ORDER_WAIT`만 냅니다(`allocation_core._walk_v5`). v4 호환 경로의 `V4_BLOCKED_BY_PRIOR_ORDER`가 오면 출력 모델이 거부합니다(DDL `at_risk_orders_ck4`) |
| T7 `delay_induced_sku_codes` | `core.delay_induced_skus(ol)` 과 같은 집합 | 정렬 |
| T7 `simulation_horizon_end` | `ol["h_end"]` | |
| T7 `expected_plan_simulation` | `ol["simE"]`의 위험 주문 부분 | 3절 |
| T6 `items[]` | `fc` 전부 | `sku_code` 순. K_SHIPMENT SKU와, 지연 유발 주문에 들어간 K_SHIPMENT 밖 SKU(엔진이 `fc`에 더함, 지연 수량 0) |
| T6 raw 값·`daily_deficits` | `raw_baseline`·`raw_total`·`raw_increment`(이미 `q4`), `base`/`total`의 `daily` | 엔진 `common.s4`와 같은 규칙(`Decimal(repr(float))` → 소수 4자리 half-even)의 문자열. 부동소수 잡음 `-0.0000`은 `0.0000` |
| T6 `min_gap` | `base`/`total`의 `min_gap` | 계산할 날이 없으면 엔진이 `inf` → `null`(9절 Q10) |
| T6 `safety_stock_quantity` | `total["ss"]` | |
| T6 `sku_scope`·영향 집합·판정 | `k_imp`·`k_watch`·`status`·`result_class` | `storage.py`와 같은 규칙(IMPACTED > WATCH > SHIPMENT_ONLY) |

## 3. `expected_plan_simulation` 형식 (계약 9절 Q12를 이 작업에서 채움)
- 엔진 T10(`options.py`)이 `simE`에서 읽는 것은 두 가지입니다: 위험 주문의 확보일(`evaluate` 107행, `_outcomes_from_sim`)과 판정일의 줄별 보호 수량(`_securement` 34~42행, 그날 출력이 없으면 마지막 날).
- 그래서 `secured_dates[]`(위험 주문 중 확보된 주문의 확보일)와 `days[]`(D부터 구간 끝까지, 날마다 **위험 주문 줄만**의 `{order_code, line_no, protected}`)로 정했습니다. 위험 주문만 담아도 T10 결과가 같고, 크기가 작습니다(overlay 위험 주문 실행: 44일·14줄, 약 2.5KB).
- 구간 끝은 `simulation_horizon_end`와 같아 따로 두지 않았습니다.
- **A의 T10 구현이 다른 값을 읽게 되면 이 형식을 다시 맞춥니다**(A 확인 요청).

## 4. 시험·검증
| 구분 | 내용 | 결과(11-06) |
| --- | --- | --- |
| 단위(엔진·DB 없음) | 숫자 규칙 6가지(half-even·잡음·음수), T7 정렬·접수 시각·대기 원인·지연 유발 SKU, 시뮬레이션 요약(위험 주문만·정렬), T6 옮기기·`min_gap` null·일별 부족 정렬, **K_SHIPMENT 밖 지연 유발 SKU**, 다른 실행 입력 거부, 지연 유발 SKU 불일치 거부, NO_DELAY 거부, 모르는 대기 원인 거부, 엔진 결과 불변·같은 값 | 16건 통과 |
| 엔진 대조(엔진 경로 + overlay DB) | A 위험 주문 실행(7줄)·B NO_RISK 실행: DB에 저장되지 않는 칸(`min_gap`·일별 부족·증분·판정·지연 유발 SKU·시뮬레이션 요약·접수 시각)을 엔진 결과와, 영향 집합·증분·위험 줄을 **판정 서명**과 대조. 저장 행과도 비교 | 2건 통과 |
| 정답 대조 `GLOWSHIP_ENGINE_GOLDEN=1` | overlay 실행 21건 중 저장 행이 있는 18건: `inventory_impact_results` **391행**·`at_risk_orders` **7행**을 엔진 `storage.py`와 같은 변환(배송일 `T23:59:59Z`, raw Decimal)으로 열마다 비교 + 같은 엔진 대조. 저장 행이 없는 3건은 NO_DELAY·보류·시간 초과 주입 실행인지 확인 | 통과(2분 37초) |
| 음성 시험(코드를 일부러 틀리게) | base·total 뒤바꾸기 → 3건 실패 / `min_gap` inf를 0으로 → 1 / 일별 부족 첫날 빼기 → 3 / K_SHIPMENT 밖 SKU 빼기 → 2 / 대기 원인 버리기 → 2 / 입력 확인 빼기 → 2 / 접수 시각을 엔진 날짜로 → 1 / 시뮬레이션 요약에 모든 주문 → 3 / `-0.0000` 정리 빼기 → 1 / 지연 유발 SKU 확인 빼기 → 1 | 모두 잡힘 |

## 5. 작업 중 실패와 원인
- **K_SHIPMENT 밖 SKU를 빼는 변이를 처음에 놓쳤습니다.** 단위 시험의 가짜 스냅샷에 `k_shipment`가 없었고, overlay 데이터에는 "지연 유발 주문에 K_SHIPMENT 밖 SKU가 들어간" 경우가 없어 정답 대조로도 드러나지 않았습니다 → 그 경우를 단위 시험으로 고정했습니다(같은 변이 → 2건 실패).
- 계획 초안에서는 `expected_plan_simulation`의 하루 출력을 SKU별 보호량으로 잡았습니다. 엔진 T10은 **줄별** 보호량을 읽으므로 그대로였다면 T10이 쓸 수 없었습니다 → `options.py`를 다시 대조해 줄 단위로 고쳤습니다.
- 정답 대조 시험에서 "저장 행이 없어도 되는 실행" 조건이 보류 실행의 실패 코드(`REQUIRED_DATA_INVALID`)를 걸렀습니다(시험 조건 실수) → 완료 상태(NO_DELAY·ON_HOLD)와 실패 코드(시간 초과)를 나눠 확인합니다.
- 시험 헬퍼에서 `await`를 생성식 안에 넣어 풀기에 실패했습니다 → 목록으로 바꿨습니다.

## 6. 남은 일
- ~~A 확인: `expected_plan_simulation` 형식이 T10 구현과 맞는지~~ → 맞음. T10(GLOW-133)이 이 형식 그대로 읽습니다(`docs/analysis/option-outcomes.md` 2절)
- 엔진 입력 캐시(고객사당 저장소 읽기 약 9.5초)
- ~~입력 검증(GLOW-100): 약속 시각이 날짜 끝이 아닌 주문(4.1절), `BUSINESS_DAY` 리드타임~~ → 정함(11-10): `BUSINESS_DAY` 리드타임은 분석 준비에서 보류(`engine-input.md` 4절 8번). 약속 시각이 날짜 끝이 아닌 주문은 보류로 막지 않고, 수집에서 날짜 끝으로 맞출지는 설계 확인 대기(계약 4.1절)
