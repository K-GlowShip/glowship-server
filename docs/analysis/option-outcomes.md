# 대응안 결과 도구 T10 (계약 입력으로 다시 계산)

- 대상: `app/modules/delay_analysis/application/agent/tools/option_outcomes.py`(도구), `infrastructure/engine/engine_loader.py`(`load_allocation_module`), 시험 `tests/modules/delay_analysis/test_option_outcomes.py`·`evaluation/test_evaluation_t10.py`
- 티켓: GLOW-133 (이슈 #62)
- 계약: `docs/agent-flow/tool-contracts.md` 6절·6.1절, 9절 Q2·Q12·Q17
- 앞 작업: T9 후보·사전 판정(GLOW-132), 재고 영향 도구 T7·T6(GLOW-126, `docs/analysis/impact-tools.md`)

## 1. 방식
- T10은 T9가 정한 후보마다 **"이렇게 하면 주문이 몇 건 살아나고 부족이 몇 개 줄어드는지"**를 계산하고, 최종 `feasibility_status`를 정합니다(9절 Q2).
- T7·T6처럼 엔진 결과(`ctx.engine.analysis`)를 옮기지 않습니다. 인터페이스 문서 2절대로 **계약 입력(T1~T9 출력)만으로 다시 계산**하고, 비교 노드가 그 결과를 엔진 판정 서명과 대조합니다(다르면 FAILED).
- 엔진 `options.evaluate`처럼 대응안마다 두 가지를 다시 돕니다.
  - **도착 창고 예측 층**(`core.forecast_sku`): 공급일이 S로 당겨지거나(출항 전 항공) 한국 항공 물량이 더해졌을 때의 부족 원값. 산술이라 서버 코드로 옮겼습니다.
  - **도착 창고 주문 배정**(`core.simulate_orders`): 출항 전 항공(S < E)과 한국 항공 보충(계산 대상)에서 D부터 구간 끝까지 날마다 배정을 다시 돌립니다. **배정은 데이터 팀 공유 배정 모듈 `allocation_core.simulate`(규칙 `D00-B-b2-v1`)를 해시 대조한 엔진 폴더에서 불러 씁니다**(9절 Q17, 사용자 결정 11-09). 배정 규칙을 서버에 다시 쓰지 않아 엔진과 어긋나지 않고, 공개 저장소에 엔진 코드를 넣지 않습니다.
- 그 밖의 규칙(판정 순서 1~8, 표 B 주문 미회복 사유, 표 C 다른 권역 대표 사유, 공여 풀, `sku_results.v1` 항목, 주문 결과 행, 집계)은 T9·T11처럼 서버 코드로 옮겼습니다.
- 동기 순수 함수입니다(`pure_tool`로 등록). DB·현재 시각·난수·언어 모델을 쓰지 않습니다. `GLOWSHIP_ENGINE_DIR`가 없으면 배정이 필요한 대응안에서 멈춥니다(엔진 입력도 같은 폴더가 있어야 만들어지므로 실제 실행에서는 생기지 않음).
- 판정 기준일의 보호 수량: 엔진은 `outs.get(j) or outs[max(outs)]`입니다. 서버는 하루 출력을 줄별 보호량 사전으로 줄여 두므로 그날 위험 줄이 없으면 빈 사전이 됩니다. `or`로 옮기면 마지막 날로 잘못 넘어가므로 날짜가 있는지로 고릅니다.
- 숫자 규칙은 엔진 `common`과 같습니다: raw 값은 `Decimal(repr(float))` → 소수 4자리 half-even, 정수화는 그 값에서 올림·내림. 감소량은 T6 문자열이 아니라 다시 계산한 부동소수 원값으로 계산합니다(엔진과 같은 값).

## 2. 계약 입력 → 엔진 스냅샷 키
| 엔진 | 계약 입력 | 쓰는 곳 |
| --- | --- | --- |
| `D`·`P`·`E`·`delayed_qty` | T1 `base_date`·`planned_3pl_inbound_date`·`expected_3pl_inbound_date`·`shipment_skus[]` | 전부 |
| `dest`·`donor`·`kr` | T1 창고 요약 | 전부 |
| `lt` | T8 `lead_times` 7개 | 공급일·배송일·판정 기준일 |
| `restore[w].on_hand`·`reserved` | T2 `warehouses[].items[]` | 예측 층·배정 첫날·공여 풀 |
| `kr_on_hand` | T2 `kr_on_hand[]`(행이 없으면 0) | 한국 항공 수량 |
| `inbound[w]` | T3 `inbound[]`(`warehouse_id`별 `arrival_date`·`quantities`) | 예측 층·배정 입고·공여 D일 입고 |
| `next_inbound` | T3 `donor_next_inbound[]`(행이 없으면 `null`) | 공여 풀 |
| `mu`·`mu_ch` | T4 `demand_totals[]` 합계 ÷ 28(창고 합계 = 두 채널 합계를 더한 정수 ÷ 28) | 예측 층·공여 수요·SEPHORA 비율 |
| `actual_demand`·`actual_known_days` | T4 `actual_demand[]`·`actual_known_days[]` | 예측 층·공여 수요(STALE 재계산) |
| `restore[w].open_orders`·`_alloc` | T5 `orders[]`(창고별, 활성 상세) | 확정 수요(`commit`)·배정 대기 주문 |
| `restore[dest].carry`·`waiting` | T5 `allocation_state[도착].pending_dispatch[]`·`waiting_order_codes[]` | 배정 첫날 입력 |
| `safety_days`·`sku_attr` | T8 `safety_stock_days`·`skus[].emergency_air_eligible` | 공여 안전재고·한국 항공 사유 |
| `fc`·`k_imp` | T6 `items[]`·`impacted_sku_codes` | SKU 항목·필요 수량. P·E 부족 원값은 다시 계산해 T6과 같은지 확인 |
| `ol.risks`·`h_end`·`simE` | T7 `risks[]`·`simulation_horizon_end`·`expected_plan_simulation` | 위험 주문, 계획 유지·다른 권역 출고의 확보 수량 |
| 공급일 S, 긴급 항공 SKU, 사전 판정 | T9 `options[]` | 대응안별 분기 |

- **이슈 #62 답(T5 `allocation_state` 형식):** 배정 다시 돌리기에는 `pending_dispatch[]`와 `waiting_order_codes[]`(+ T5 `orders[]`의 줄)로 충분합니다. `blocked_order_protection[]`·`secured_waiting_for_dispatch[]`·`replay_result_sha256`은 T10이 읽지 않습니다. 모델은 `list[dict]` 그대로 두었습니다(형식 모델로 바꿀지는 PR 리뷰에서 정함). → 실행 묶음 채우기(GLOW-128)에서 형식 모델(`AllocationState`)로 바꿈. T10은 모델 속성으로 읽고, 엔진에 넘기는 출고 대기 주문만 `model_dump(mode="json")`으로 되돌립니다(모드를 빼면 확보일이 `date`로 넘어가 배정 모듈이 결과 해시를 만들다 `TypeError`).
- **`impact-tools.md` 6절 답(T7 `expected_plan_simulation` 형식):** 계획 유지와 다른 권역 출고가 읽는 값(위험 주문 확보일, 판정 기준일의 줄별 보호량)으로 충분합니다. 형식을 바꾸지 않았습니다.
- 배정 입력의 고객사 코드는 자리 값(`T10`)입니다. 배정 모듈은 이 값을 범위 판정(모든 주문이 같은 고객사·창고인지)에만 쓰고, T10이 읽는 확보일·보호량에는 영향이 없습니다.

## 3. 입력 대조 (멈춤 = `ValueError`, 재시도 없이 FAILED)
| 대조 | 왜 |
| --- | --- |
| 다시 계산한 P·E 부족 원값 = T6 `raw_baseline_shortage`·`raw_total_shortage` | 다른 실행이나 다른 순서의 T6을 넘겼으면 감소량이 틀림 |
| 다른 권역 출고의 "K_IMPACTED 전부 다음 입고 없음" = T9 `DONOR_NO_NEXT_INBOUND` | T3·T6을 T9와 다르게 넘긴 경우 |
| `FEASIBLE`·`NOT_FEASIBLE` 대응안의 `outcomes` 행 수 = T7 위험 상세 수, 회복 + 미회복 = 위험 주문 수 | 계약 6절 끝(엔진 `bundle_verify.py` `COMPUTED_OUTCOME_ROWS`·`ORDER_COUNT_MISMATCH`) |
| 도착 창고 출고 처리·고객 배송 리드타임이 있음 | 없으면 엔진은 보류(`MISSING_REQUIRED_DATA`)라 T10까지 오지 않음 |

## 4. 시험·검증
| 구분 | 내용 | 결과(11-09) |
| --- | --- | --- |
| 단위(엔진·DB 없음) | 손으로 만든 입력(주문 2건·SKU 2개)과 가짜 배정기: 계획 유지가 T7 요약을 읽는 것(판정 기준일 보호 4개 → 부분 확보), 한국 항공의 배정 첫날 입력(정책·대기 주문·지연 선적 입고·구간)과 회복, 공여 풀, 판정 순서 7번(Q9), 리드타임 없음, 입력 대조 3가지 | 6건 통과 |
| 엔진 대조 — overlay | 대응안이 있는 실행 **15건(대응안 42개)**: T1~T9를 그래프 순서로 불러 T10 출력을 엔진 `analysis["options"]`를 `storage.py`와 같은 변환으로 옮긴 값과 **통째로**(대응안 행·SKU 항목·주문 결과) 비교 | 통과(1분) |
| 엔진 대조 — 평가 시나리오 | 대응안이 있는 시나리오 10건(GS-02·03·05·07·08·09A·09B·10·11·11 탐침) + 변형 2건(리드타임 없음, 출항 전 항공 S ≥ E). 출항 전 항공·한국 항공 배정으로 회복, 공여 전부 판정 불가, 여러 SKU 주문, 공여 풀을 약속 순서로 나눠 씀을 거침 | 12건 통과 |
| 정답 대조 `GLOWSHIP_ENGINE_GOLDEN=1` | overlay에 저장된 `response_options` **39행(14실행)**·`option_order_outcomes` **21행**과 열마다(`sku_results` 포함) 비교. 저장 행이 없는 1실행은 시간 초과 주입 실행인지 확인 | 통과(1분) |
| 음성 시험(코드를 일부러 틀리게, 위 두 파일) | 한국 항공 공급 창고를 도착 창고로 → 9건 실패 / 대표 사유 우선순위 바꾸기 → 1 / 음수 가용일 때 부족 규칙 빼기 → 18 / 공여 안전재고 빼기 → 11 / 공여 주문 순서를 주문 코드로 → 1 / 감소량에서 기준선 빼기 생략 → 10 / 보호 수량 판정일을 D로 → 11 | 7건 잡힘, 3건 남음(아래) |

음성 시험에서 남은 3건:
- **출항 전 항공의 "계획 유지로 이미 회복" 판정 빼기:** 위험 주문은 정의상 E 시뮬레이션에서 약속을 넘기므로 이 판정은 늘 거짓입니다(엔진 주석 "KEEP 기준: 위험 주문은 E에서 회복되지 않음"). 빼도 결과가 같은 코드라 시험으로 잡을 수 없습니다. 엔진과 모양을 맞추려고 남겼습니다.
- **판정 순서 6·7 바꾸기:** 확정 감소 원값이 0보다 크고 1 미만이면서 대표 사유가 `DONOR_NO_NEXT_INBOUND`인 경우에만 갈립니다. overlay·시나리오·단위 입력에 없는 조합입니다.
- **한국 항공 수량을 공급일 전 부족까지 보기:** 일별 부족은 누적 수요라 늘어나므로, 위험 주문이 남는 입력에서는 공급일 뒤 최댓값이 전체 최댓값과 같습니다(GS-03에 도착 입고를 더해 봐도 위험 주문이 남는 동안은 같음).

## 5. 작업 중 실패와 원인
- **overlay만으로는 배정 경로를 거의 보지 못했습니다.** overlay 대응안 42개 중 주문 결과가 있는 것은 1실행(위험 상세 7줄)뿐이고, 배정을 다시 돌린 대응안(출항 전 항공·한국 항공 계산 6개)은 모두 위험 주문이 없는 실행이었습니다. 처음 대조가 통과했을 때 경로별 개수를 세어 보고 알았습니다 → 평가 시나리오 세계(엔진 fixture)를 T1~T9 출력으로 옮기는 시험 어댑터(`evaluation_adapters.outcomes_input`)를 만들어 회복 경로를 엔진과 대조했습니다.
- **시나리오에도 없는 경로가 남았습니다**(리드타임 없음, S ≥ E) → 시나리오 세계를 조금 바꿔 엔진을 다시 돌리는 변형 2건을 더했습니다.

## 6. 남은 일
- ~~실제 도구 교체(등록표에 가짜 T10 대신 이 함수를 `pure_tool`로)~~ — 실제 실행 묶음 `run_start/engine_runtime.py` `ENGINE_TOOLS`에 `pure_tool(calculate_option_outcomes)`로 등록함(GLOW-128 묶음 채우기)
- ~~T5 `allocation_state` 모델을 형식이 있는 모델로 바꿀지(이슈 #62 세 번째 항목, 9절 Q12)~~ — 형식 모델로 바꿈(GLOW-128 묶음 채우기, 계약 3.5절 표)
