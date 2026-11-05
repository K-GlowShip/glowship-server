# 에이전트 도구 입출력 계약 (계약 10, 초안)

분석 에이전트 도구 T1~T12의 **이름과 입력·출력 JSON 모양**을 정합니다. 업무 분장 3절 계약 10이고, agentflow 9절 "팀이 확정할 사항"의 첫 항목("실제 도구 함수명과 도구별 입력·출력 JSON 스키마")입니다.

- **상태:** 모델 반영판(11-04, GLOW-112 두 번째 PR)입니다. 9절 Q2~Q9는 설계 담당 결정을, 조회 기준(순번)과 `NOT_FEASIBLE` 사유 규칙(6.1절)은 A 리뷰를 반영했습니다. 이 문서대로 Pydantic 모델(`app/modules/delay_analysis/application/agent/tools/`)을 만들었고, 문서 표와 모델 필드·코드값이 같은지는 시험(`tests/modules/delay_analysis/test_tool_schemas.py`)이 확인합니다. 문서를 고치면 모델도 같이 고쳐야 시험이 통과합니다.
- **1차 범위:** T9·T10·T11입니다. 이 세 도구의 입력은 조회 도구 T1~T5·T8과 계산 도구 T6·T7의 출력이라서, 그 출력 모양도 이 문서에서 함께 정합니다. T12는 개요만 적습니다(8절).
- **정하지 않는 것:** 계산 방법 자체입니다. 계산은 데이터 팀 엔진 참조 구현을 이식하고(업무 분장 2절), 이 문서는 함수 경계에서 주고받는 값만 정합니다.

## 0. 근거 자료

| 자료 | 위치·판 | 쓰는 곳 |
| --- | --- | --- |
| agentflow | glowship-plan `agentflow_glowship.md` 4절(도구 명세·계산 규칙), 5.3절, 9절 | 도구 번호·이름(가칭)·호출 주체, 계산·판정 규칙 |
| 업무 분장 | `backend_업무분장_glowship_수정안_(v6.2).md` 2절 C, 3절 계약 10, 5절 계산 도구 경계 | 구현 담당, 도구 안 DB 쓰기·언어 모델 호출 금지 |
| 엔진 참조 구현 | 데이터 팀 `04_적재검증도구/glowship_engine` — `snapshot.py`(입력 스냅샷), `core.py`(예측 층·주문 층·영향 집합·결과 분류), `options.py`(대응안 평가·추천), `pipeline.py`(집계), `storage.py`(저장 행) | 입출력 필드와 판정 순서 |
| 주문 배정 모듈 | glowship-plan `838092b` `tools/synth2025_v5/allocation_core.py`(규칙 `D00-B-b2-v1`). 공유 묶음에는 없고 11-04에 데이터 팀이 추가 | 엔진 `core.py`·`snapshot.py`가 불러오는 배정 재생·시뮬레이션 |
| DDL | `erd_glowship_postgres_v6_2_analysis_ddl.sql` | 코드값(CHECK), 출처 열 |
| 분석 JSON 계약 v3 | `05_정답계약검증/계약/analysis_json_contract_v3_adopted.md` 0절·1절 | 공통 JSON 규칙, `sku_results.v1` |

엔진 파일 해시(sha256 앞 12자리, 11-03 확인): `options.py` `974577bcc337`, `core.py` `8a88f18285cb`, `common.py` `514d6a7d9901`. 배정 모듈(11-04 확인): `allocation_core.py` `fcb08cf8dd2e`(overlay manifest와 같음).

**엔진 수정판(11-04):** 설계 담당이 9절 Q9 결정으로 `options.py`의 `evaluate` 한 분기를 고쳤습니다(glowship-plan `b80fd04`·`11b902e` `tools/glowship_engine/options.py` `f66e319c6bdd`, 새 시험 `test_q9_donor_reason.py` 6건). 위 `974577bcc337`은 수정 전 판이고, 이 문서의 5~7절은 수정판 기준입니다.

## 1. 공통 규칙

| 규칙 | 내용 |
| --- | --- |
| 도구 유형 | **조회 도구**(T1·T2·T3·T4·T5·T8)는 DB를 읽기만 합니다. **계산·검증 도구**(T6·T7·T9·T10·T11·T12)는 입력만으로 계산하는 순수 함수입니다. DB·현재 시각·난수·언어 모델을 쓰지 않습니다(엔진 `core.py` 머리말, 업무 분장 5절) |
| 범위 | 조회 도구는 실행의 고객사(`company_id`) 범위에서만 읽습니다. 워커 역할 세션에 `app.company_id`를 설정하고, 조회 조건에도 `company_id`를 넣습니다(GLOW-98 실행 상태 조회기와 같은 방식) |
| 입력 기준(컷오프) | 조회 도구는 **실행의 입력 순번(`analysis_runs.input_seq`) 이하로 적용된 수집**(`data_sync_runs.applied_seq <= input_seq`)이 적재한 값만 읽습니다. serviceflow 5.2절 "왜 시각이 아니라 순번인가"와 STALE 판정(순번 기준)이 같은 기준을 쓰게 하기 위해서입니다. 엔진의 `confirm_t`(시각)는 CSV를 읽는 참조 구현의 대응일 뿐이고, 서버는 순번을 씁니다 |
| 저장 | 도구는 업무 DB에 쓰지 않습니다. 결과 저장은 서비스가 합니다(agentflow 4절 "부작용 없음") |
| JSON 키 | snake_case입니다. 엔진과 DB 열 이름을 그대로 써서 저장 단계에서 키를 바꾸지 않게 합니다(D20과 같은 방향) |
| 값 모양 | 분석 JSON 계약 v3 0절을 따릅니다. 식별자는 UUID 소문자 하이픈 문자열, 날짜는 `YYYY-MM-DD`(UTC 달력일), 시각은 `YYYY-MM-DDTHH:MM:SSZ`, 수량은 개(EA) 정수, raw 값은 소수 4자리 10진 문자열입니다. 결측은 키를 빼지 않고 `null`로 둡니다 |
| 배열 | 아래 표마다 적은 정렬 키로 오름차순입니다. 엔진은 SKU를 SKU 코드로 묶어 다루므로, 이 문서도 SKU 배열은 `sku_code` 순으로 둡니다 |
| 코드값 | DDL CHECK와 같은 값입니다. 서버는 `app/modules/delay_analysis/infrastructure/analysis_enums.py`의 열거형을 씁니다 |
| 모델 | 도구마다 입력·출력 Pydantic 모델이 있습니다(`application/agent/tools/`의 `lookup_schemas.py` T1~T5·T8, `impact_schemas.py` T7·T6, `option_schemas.py` T9~T11). T7·T10·T11 출력은 저장 표의 DDL CHECK 조합을, T6 출력은 결과 분류(`analysis_runs_ck21`)를 모델이 먼저 확인합니다. T6 저장 열(`inventory_impact_results`)의 CHECK는 그 열이 모델에 없어 저장 단계에서 확인합니다(9절 Q11). 배열 정렬은 도구가 맞추고 모델은 검사하지 않습니다. 다만 코드·날짜 값 배열(T4 `event_names`·`actual_known_days`, T6 입력 `delay_induced_sku_codes`, T6 `impacted_sku_codes`·`watch_sku_codes`, T7 `delay_induced_sku_codes`, T9 `eligible_sku_codes`)은 엔진이 집합에서 만들므로 같은 값이 두 번 있으면 모델이 거부합니다(`common.UniqueCodes`·`UniqueDates`) |
| 날짜 판정 | 엔진은 시각 값(`promised_delivery_at`, `ordered_at`)을 앞 10자리(UTC 날짜)로 잘라 날짜끼리 비교합니다(`common.d`). 도구도 같은 방식으로 판정합니다 |
| 평균 수요 | 일평균을 소수로 넘기지 않고, **기간 합계(정수)와 일수**를 넘깁니다. 엔진은 합계 ÷ 28을 부동소수로 계산해 바로 쓰므로, 반올림한 평균을 넘기면 정수화 경계에서 결과가 달라질 수 있습니다 |
| 이름 | agentflow 4절의 도구 이름(가칭)을 정식 이름으로 씁니다. 새 이름을 만들지 않아 문서·코드가 바뀌는 곳을 줄입니다 |

## 2. 도구 목록

| 번호 | 이름 | 유형 | 구현 | 호출 노드(현재 그래프) | 엔진 대응 | 이 문서 |
| --- | --- | --- | --- | --- | --- | --- |
| T1 | `get_delay_context` | 조회 | C | 조정 에이전트 | `snapshot.build`의 지연 문맥 부분 | 3.1 |
| T2 | `get_inventory_snapshot` | 조회 | C | 재고 영향 | `ReplayReservationSource.restore`의 재고 부분, `kr_on_hand` | 3.2 |
| T3 | `get_inbound_schedule` | 조회 | C | 재고 영향 | `snapshot.build` `inbound()`, `next_inbound` | 3.3 |
| T4 | `get_sales_history` | 조회 | C | 재고 영향 | `snapshot.build` `mu()`, `window_overlap` | 3.4 |
| T5 | `get_open_orders` | 조회 | C | 재고 영향 | `restore`의 열린 주문, `to_alloc_order`, D−1 배정 재생 | 3.5 |
| T8 | `get_operating_rules` | 조회 | C | 재고 영향·대응안 | `snapshot.build`의 `rule`·`lt` | 3.6 |
| T7 | `identify_at_risk_orders` | 계산 | C | 재고 영향 | `core.order_layer` | 4.1 |
| T6 | `calculate_inventory_impact` | 계산 | C | 재고 영향 | `core.forecast_layer`·`impact_sets`·`classify` | 4.2 |
| T9 | `check_option_feasibility` | 계산 | **A** | 대응안 | `options.candidates`와 `evaluate`의 사전 판정 부분 | **5** |
| T10 | `calculate_option_outcomes` | 계산 | **A** | 대응안 | `options.evaluate`, `pipeline.aggregates` | **6** |
| T11 | `compare_options` | 계산 | **A** | 대응안 비교 | `options._rank` | **7** |
| T12 | `validate_agent_output` | 검증 | C | 결과 검증 | (2차) | 8 |

T7을 T6보다 먼저 적은 것은 실행 순서 때문입니다(4절). 번호는 agentflow 그대로 둡니다.

## 3. 조회 도구 출력 — 분석 입력 스냅샷

엔진은 분석 입력을 스냅샷 하나(`snapshot.build`)로 고정한 뒤 계산 함수가 그 일부를 읽습니다. 서버에서는 이 스냅샷을 조회 도구 6개의 출력으로 나눕니다. 모든 조회는 **입력 순번까지 적용된 값**만 읽습니다(1절 "입력 기준"). 아래 표의 "최신 관측"도 입력 순번 이하 수집이 적재한 관측 가운데 가장 최근 것입니다.

### 3.1 T1 `get_delay_context` — 지연 문맥

입력: `analysis_run_id`, `delay_event_id`, `base_date`(D), `input_seq`. D와 입력 순번은 실행 생성(GLOW-99)이 정합니다.

| 필드 | 형식 | NULL | 뜻 | DB 출처 | 엔진 키 |
| --- | --- | --- | --- | --- | --- |
| `shipment_id` | uuid | 아니오 | 지연 선적 | `delay_events.shipment_id` | `delayed_shipment_id` |
| `base_date` | date | 아니오 | 분석 기준일 D | 입력 | `D` |
| `planned_3pl_inbound_date` | date | 아니오 | 계획 3PL 입고일 P | `shipments.planned_3pl_inbound_date` | `P` |
| `expected_3pl_inbound_date` | date | 아니오 | 예상 3PL 입고일 E (입력 순번까지의 최신 관측) | `shipment_schedule_snapshots.expected_3pl_inbound_date` | `E` |
| `stage` | text | 아니오 | 운송 단계 (입력 순번까지의 최신 관측) | `shipment_schedule_snapshots` | `stage` |
| `transport_mode` | text | 아니오 | `OCEAN`·`AIR` | `shipments.transport_mode`(ck1) | `mode` |
| `planned_kr_release_date` | date | 아니오 | 한국 출고 예정일 (출항 전 항공의 공급일 계산) | `shipments.planned_kr_release_date` | `planned_kr_release` |
| `trigger_shipment_schedule_snapshot_id` | uuid | 아니오 | 입력 순번까지의 최신 관측 | `shipment_schedule_snapshots` | `trigger_snapshot_id` |
| `shipment_skus[]` | 배열(`sku_code`) | — | 지연 선적의 SKU = K_SHIPMENT | `shipment_lines` + `skus` | `k_shipment`, `delayed_qty` |
| `shipment_skus[].sku_id`·`sku_code` | uuid·text | 아니오 | | `skus` | `sku_ids` |
| `shipment_skus[].delayed_quantity` | 정수 | 아니오 | SKU별 선적 수량 합 | `shipment_lines.shipped_quantity` 합 | `delayed_qty` |
| `destination_warehouse` | 창고 요약 | 아니오 | 도착 창고 | `shipments.warehouse_id` → `warehouses` | `dest` |
| `donor_warehouse` | 창고 요약 | 아니오 | 공여 창고 = 도착 창고가 아닌 `US_3PL` 창고 하나 (9절 Q4) | `warehouses`(ck1) | `donor` |
| `kr_origin_warehouse` | 창고 요약 | 아니오 | 한국 창고 (`KR_ORIGIN`) | `warehouses` | `kr` |

창고 요약 = `warehouse_id`, `warehouse_code`, `region_code`(`KR`·`WEST`·`EAST`, ck2). 미국 창고가 셋 이상인 고객사(공여 후보가 둘 이상)는 범위 밖이고, 생기면 대응안 규칙부터 다시 정합니다(9절 Q4).

### 3.2 T2 `get_inventory_snapshot` — D−1 마감 재고

| 필드 | 형식 | NULL | 뜻 | DB 출처 | 엔진 키 |
| --- | --- | --- | --- | --- | --- |
| `inventory_date` | date | 아니오 | D−1 | — | `Dm1` |
| `warehouses[]` | 배열(`warehouse_code`) | — | 도착·공여 창고 2개 | | `restore[wh]` |
| `warehouses[].items[]` | 배열(`sku_code`) | — | 고객사의 모든 SKU | `inventory_daily`(D−1) | |
| `…items[].on_hand_quantity` | 정수 | 아니오 | 보유 | `inventory_daily.on_hand_quantity` | `on_hand` |
| `…items[].reserved_quantity` | 정수 | 아니오 | 예약 | `inventory_daily.reserved_quantity` | `reserved` |
| `kr_on_hand[]` | 배열(`sku_code`) `{sku_code, on_hand_quantity}` | — | 한국 창고 보유 (행이 있는 SKU만) | `inventory_daily`(한국 창고, D−1) | `kr_on_hand` |

도착·공여 창고에 D−1 행이 없는 SKU가 있거나 가용 재고가 음수면, 엔진은 분석을 보류합니다(`REQUIRED_DATA_INVALID`). 이 확인은 입력 검증(GLOW-100)이 합니다.

### 3.3 T3 `get_inbound_schedule` — 입고 예정

| 필드 | 형식 | NULL | 뜻 | DB 출처 | 엔진 키 |
| --- | --- | --- | --- | --- | --- |
| `inbound[]` | 배열(`warehouse_code`, `arrival_date`, `shipment_id`) | — | 도착·공여 창고의 입고 예정 선적. 지연 선적과 입력 순번까지 이미 `RECEIVED`인 선적은 뺌 | `shipments`·`shipment_schedule_snapshots`·`shipment_lines` | `inbound[wh]` |
| `inbound[].shipment_id`·`warehouse_id` | uuid | 아니오 | | | |
| `inbound[].expected_3pl_inbound_date` | date | 아니오 | 최신 관측 예상일 | `shipment_schedule_snapshots` | `raw_expected` |
| `inbound[].arrival_date` | date | 아니오 | 계산에 쓰는 도착일 = max(예상일, D) | — | `arrival` |
| `inbound[].quantities[]` | 배열(`sku_code`) `{sku_code, quantity}` | — | | `shipment_lines.shipped_quantity` | `qty` |
| `donor_next_inbound[]` | 배열(`sku_code`) `{sku_code, next_inbound_date}` | 날짜는 예 | 공여 창고에 그 SKU가 들어오는 가장 이른 예상일(≥ D). 없으면 `null` (R-03) | 위와 같음 | `next_inbound` |

### 3.4 T4 `get_sales_history` — 최근 28일 유효 주문 수요

| 필드 | 형식 | NULL | 뜻 | DB 출처 | 엔진 키 |
| --- | --- | --- | --- | --- | --- |
| `sales_basis_type` | text | 아니오 | `ORDER_DEMAND` 고정(D9) | — | — |
| `basis_start_date`·`basis_end_date` | date | 아니오 | D−28 ~ D−1 | — | `sales_basis.start`·`end` |
| `basis_days` | 정수 | 아니오 | 28 고정(계산 계약 `T14-v1`, 9절 Q6) | — | 엔진 고정값 |
| `demand_totals[]` | 배열(`warehouse_code`, `sku_code`, `sales_channel`) | — | 도착·공여 창고 × 고객사 SKU × 채널(`AMAZON`·`SEPHORA`)별 28일 유효 주문 수요 합 | `orders`·`order_lines` | `mu`·`mu_ch`의 분자 |
| `demand_totals[].quantity` | 정수 | 아니오 | 합계. 일평균 = `quantity / basis_days` | | |
| `event_days`·`event_names[]` | 정수·배열 | 아니오 | 28일 창과 겹치는 행사 일수·이름(경고용, 수요 보정 안 함) | 행사 일정 설정 | `sales_basis.event_days`·`event_names` |
| `calendar_version`·`calendar_sha256` | text | 아니오 | 행사 일정 설정 판 | 설정 파일 | `sales_basis.calendar_*` |
| `actual_known_days[]`·`actual_demand[]` | 날짜 배열 · 배열(`warehouse_code`, `sku_code`, `demand_date`) `{warehouse_code, sku_code, demand_date, quantity}` | — | STALE 재계산 때만. 입력 순번 뒤에 적용된 수집의 그날 실제 수요 | `orders` | `actual_known_days`·`actual_demand` |

창고·SKU 합계(채널 무시)는 두 채널 행을 더해 얻습니다. 일반 실행에서는 `actual_*` 두 배열이 비어 있습니다.

### 3.5 T5 `get_open_orders` — D−1 마감 열린 주문과 배정 재생 상태

| 필드 | 형식 | NULL | 뜻 | DB 출처 | 엔진 키 |
| --- | --- | --- | --- | --- | --- |
| `orders[]` | 배열(`order_code`) | — | 도착·공여 창고에서 D−1 마감에 열린 주문. 취소·원천 삭제 주문과 D−1 뒤 접수 주문은 뺌 | `orders`, 출고일(수집 기록) | `restore[wh].open_orders` |
| `orders[].order_id`·`order_code`·`warehouse_id` | uuid·text·uuid | 아니오 | | `orders` | |
| `orders[].sales_channel` | text | 아니오 | `AMAZON`·`SEPHORA`(ck2) | `orders.sales_channel` | |
| `orders[].ordered_at`·`promised_delivery_at` | 시각 | 아니오 | | `orders` | |
| `orders[].lines[]` | 배열(`line_no`) | — | | `order_lines` | `lines_by_order`, `_alloc` |
| `…lines[].order_line_id`·`line_no`·`sku_id`·`sku_code` | | 아니오 | | | |
| `…lines[].ordered_quantity` | 정수 | 아니오 | | `order_lines.ordered_quantity` | |
| `…lines[].line_status` | text | 아니오 | `ACTIVE`·`REMOVED_AT_SOURCE`(ck3). 배정에는 `ACTIVE`만 씀 | `order_lines.line_status` | |
| `allocation_state[]` | 창고별 | — | D−1 배정 하루 재생 결과. 배정 모듈 원본은 확보했고(9절 Q1), 필드 형식은 다음 PR(엔진 연결)에서 채웁니다 | 엔진 `allocation_core.allocate_day` | `restore[wh]`의 `carry`·`waiting`·`protection`·`secured_wait`·`line_state` |

`allocation_state`는 DB에 저장된 값이 아니라 배정기를 하루 다시 돌려 얻는 값입니다. 엔진은 재생 결과의 예약 수량이 DB `reserved_quantity`와 다르면 보류합니다.

### 3.6 T8 `get_operating_rules` — 운영 기준

| 필드 | 형식 | NULL | 뜻 | DB 출처 | 엔진 키 |
| --- | --- | --- | --- | --- | --- |
| `operating_rule_set_id`·`version_no` | uuid·정수 | 아니오 | D 시점 유효한 최신 버전(`effective_from` ≤ D) | `operating_rule_sets` | `rule` |
| `safety_stock_days` | 정수 | 아니오 | | `operating_rule_sets.safety_stock_days` | `safety_days` |
| `sales_basis_days` | 정수 | 아니오 | 계산은 28 고정. 이 값이 28이 아니면 입력 검증이 실행을 `ON_HOLD`(`REQUIRED_DATA_INVALID`)로 막습니다(9절 Q6) | `operating_rule_sets.sales_basis_days` | `basis_days` |
| `lead_times.dest_outbound_handling_days` | 정수 | 예 | 도착 창고 출고 처리 | `operating_lead_times` `OUTBOUND_HANDLING`, 도착 창고 | `lt.oh_dest` |
| `lead_times.dest_customer_delivery_days` | 정수 | 예 | 도착 창고 → 도착 권역 고객 배송 | `CUSTOMER_DELIVERY`, 도착 창고, `to_region_code` = 도착 권역 | `lt.cd_same` |
| `lead_times.donor_outbound_handling_days` | 정수 | 예 | 공여 창고 출고 처리 | `OUTBOUND_HANDLING`, 공여 창고 | `lt.oh_donor` |
| `lead_times.donor_customer_delivery_days` | 정수 | 예 | 공여 창고 → 도착 권역 고객 배송 | `CUSTOMER_DELIVERY`, 공여 창고, 도착 권역 | `lt.cd_cross` |
| `lead_times.kr_outbound_handling_days` | 정수 | 예 | 한국 창고 출고 처리 | `OUTBOUND_HANDLING`, 한국 창고 | `lt.oh_kr` |
| `lead_times.kr_air_transit_days` | 정수 | 예 | 한국 → 도착 권역 항공 | `AIR_TRANSIT`, 한국 창고, 도착 권역 | `lt.air_kr` |
| `lead_times.dest_inbound_handling_days` | 정수 | 예 | 도착 창고 입고 처리 | `INBOUND_HANDLING`, 도착 창고 | `lt.inbound_dest` |
| `skus[]` | 배열(`sku_code`) `{sku_id, sku_code, emergency_air_eligible}` | 아니오 | 긴급 항공 가능 여부 | `skus.emergency_air_eligible`(boolean) | `sku_attr` |

리드타임 `null` = 운영 기준에 그 행이 없음입니다. **`day_basis = BUSINESS_DAY`인 행도 `null`로 둡니다.** 계산 계약 `T14-v1`은 달력일만 계산하기 때문입니다(9절 Q5). 도착 창고의 출고 처리나 고객 배송이 `null`이면 분석 전체를 보류하고(엔진 `MISSING_REQUIRED_DATA`, 실행 `ON_HOLD`), 나머지가 `null`이면 해당 대응안만 `NEEDS_CHECK`·`MISSING_OPERATING_RULE`입니다(5절). `MISSING_REQUIRED_DATA`는 실행 보류 사유로만 쓰고 대응안 사유로는 쓰지 않습니다(9절 Q7). `skus[]`는 SKU 속성이라 T8에 두었습니다.

## 4. 재고 영향 계산 도구 출력 (C 구현)

T9~T11이 읽는 값만 적습니다. 저장 열은 `inventory_impact_results`·`at_risk_orders`(분석 실행 상세 응답 스키마 문서 참고)와 같습니다.

### 4.1 T7 `identify_at_risk_orders` — 위험 주문 (먼저 실행)

입력: T1·T2·T3·T5·T8. 계획일 P와 예상일 E로 도착 창고 배정을 두 번 시뮬레이션하고, E에서 약속 배송일을 넘는 주문을 위험 주문으로 고릅니다(P에서도 넘으면 `PRE_EXISTING`, 아니면 `DELAY_INDUCED`). 시뮬레이션은 `allocation_core.simulate`를 씁니다. 원본은 확보했고, `expected_plan_simulation`의 필드 형식은 다음 PR에서 채웁니다(9절 Q1).

| 필드 | 형식 | NULL | 뜻 | 엔진 키 |
| --- | --- | --- | --- | --- |
| `risks[]` | 배열(`order_code`) | — | 위험 주문(주문 단위) | `ol.risks` |
| `risks[].order_id`·`order_code`·`sales_channel` | | 아니오 | | |
| `risks[].ordered_at`·`promised_delivery_at` | 시각 | 아니오 | | `ordered`, `promised_at` |
| `risks[].risk_attribution` | text | 아니오 | `PRE_EXISTING`·`DELAY_INDUCED` | `attribution` |
| `risks[].expected_delivery_date`·`baseline_expected_delivery_date` | date | 아니오 | E·P 시뮬레이션의 배송일 | `delivery_E`·`delivery_P` |
| `risks[].expected_beyond_horizon` | boolean | 아니오 | 시뮬레이션 구간 안에서 확보되지 않음 | `beyond` |
| `risks[].lines[]` | 배열(`line_no`) `{order_line_id, line_no, sku_code, open_quantity, wait_cause}` | `wait_cause`만 예 | 활성 상세 | `lines` |
| `delay_induced_sku_codes[]` | 배열 | — | `DELAY_INDUCED` 주문의 모든 활성 상세 SKU(K_SHIPMENT 밖 포함) | `core.delay_induced_skus` |
| `simulation_horizon_end` | date | 아니오 | 시뮬레이션 구간 끝 = max(E, 약속일들) + 14일 | `h_end` |
| `expected_plan_simulation` | 객체(엔진 연결에서 정함, 9절 Q12) | — | E 시뮬레이션 결과(주문별 확보일, 일별 보호량). T10의 `KEEP_PLAN`·다른 권역 출고가 씀 | `simE` |

저장할 때 배송일은 `T23:59:59Z` 시각이 됩니다(엔진 `storage.py`). 그래서 모델은 그 시각으로 DDL을 확인합니다: 예상 배송 > 약속(`at_risk_orders_ck2`), `PRE_EXISTING`이면 기준선 배송 > 약속(ck5), `DELAY_INDUCED`이면 기준선 배송 ≤ 약속(ck6). 엔진은 날짜로 비교하므로 약속 시각이 `T23:59:59Z`가 아니면 두 비교가 갈릴 수 있습니다. 개발 DB(v6.2 + 2026 H1 적재)의 주문 171,888건(2025-01-01 ~ 2026-06-30 접수, 고객사 2곳)은 약속 시각이 모두 `23:59:59Z`입니다(11-04 확인). 날짜 끝이 아닌 약속 시각이 들어오는 원천이 생기면 갈리므로, 입력 검증(GLOW-100)에서 이런 주문이 있으면 실행을 보류로 막을지 정합니다(엔진에는 없는 검사라 그 작업에서 결정, #26 리뷰).

### 4.2 T6 `calculate_inventory_impact` — 예측 층·영향 집합·결과 분류

입력: T1·T2·T3·T4·T5·T8, 그리고 T7의 `delay_induced_sku_codes`. T5는 도착 창고 열린 주문의 활성 상세 수량을 미출고 확정 수량으로 쓰기 때문에 필요합니다(`core.forecast_sku`의 `commit`). 영향 집합(K_IMPACTED)에 지연 유발 주문의 SKU가 들어가므로 **T7 다음에 실행**합니다(엔진 순서 `forecast_layer` → `order_layer` → `impact_sets` → `classify`와 결과가 같습니다. 9절 Q3).

| 필드 | 형식 | NULL | 뜻 | 엔진 키 |
| --- | --- | --- | --- | --- |
| `items[]` | 배열(`sku_code`) | — | SKU별 예측 층. K_SHIPMENT SKU와, K_SHIPMENT 밖이지만 지연 유발 위험 주문에 들어간 SKU(지연 수량 0, `IMPACTED`)입니다(엔진 `pipeline.analyze` 70~72행) | `fc` |
| `items[].sku_id`·`sku_code`·`sku_scope` | | 아니오 | `IMPACTED`·`WATCH`·`SHIPMENT_ONLY` | `impact_sets` |
| `items[].raw_baseline_shortage`·`raw_total_shortage`·`raw_increment_shortage` | raw | 아니오 | P 기준·E 기준·증분 부족 원값 | `raw_baseline`·`raw_total`·`raw_increment` |
| `items[].baseline_prepare_quantity`·`increment_prepare_quantity`·`total_prepare_quantity` | 정수 | 아니오 | 원값 올림, 합계 = 앞 둘의 합 | `baseline_prepare`·`increment_prepare`·`total_prepare` |
| `items[].safety_stock_quantity` | 정수 | 아니오 | 올림(일평균 × 안전재고 일수) | `total.ss` |
| `items[].baseline`·`total` | 객체 | — | `stockout_date`, `stockout_days`, `safety_stock_breach_date`, `min_gap`(raw, 음수 가능), `daily_deficits[]`(`{date, deficit}`, D ≤ date < E, raw). 품절·안전재고 하회가 없으면 그 날짜는 `null`, 계산할 날이 없으면(D ≥ 입고일) `min_gap`은 `null`(9절 Q10) | `base`·`total` |
| `impacted_sku_codes[]`·`watch_sku_codes[]` | 배열 | — | K_IMPACTED, K_WATCH | `k_imp`·`k_watch` |
| `completed_status`·`result_class` | text | 아니오 | `READY_FOR_REVIEW`·`NO_RISK`와 결과 분류 4종 | `classify` |

`daily_deficits`는 대응안 계산에 필요합니다. 한국 창고 항공은 공급일 이후 가장 큰 일별 부족만큼 보냅니다(`options.evaluate` `KR_AIR_REPLENISH`).

## 5. T9 `check_option_feasibility` — 대응안 후보와 사전 판정 (A 구현)

**결정(9절 Q2):** T9는 계산 전에 정할 수 있는 것만 정합니다. **최종 `feasibility_status`는 T10이 정합니다.**
엔진에서는 `KEEP_PLAN` 외 대응안의 `FEASIBLE`이 "지연 유발 주문 회복 1건 이상 또는 확정 감소 1개 이상"(`response_options_ck12`)으로 정해지고, 이것은 대응안을 계산해 봐야 압니다(`options.evaluate` 169~192행). agentflow 4절 T9 설명(`FEASIBLE`·`NOT_FEASIBLE`·`NEEDS_CHECK`)은 설계 담당이 "후보·사전 판정"으로 고칩니다.

입력

| 필드 | 출처 |
| --- | --- |
| `transport_mode`, `stage`, `base_date`(D), `expected_3pl_inbound_date`(E), `planned_kr_release_date` | T1 |
| `lead_times` 7개 | T8 |
| `impacted_sku_codes[]` | T6 |
| `skus[].emergency_air_eligible` | T8 |
| `kr_on_hand[]` | T2 |
| `donor_next_inbound[]` | T3 |

출력: `options[]` — 후보 대응안만, 고정 순서(`KEEP_PLAN` → `PRE_DEPARTURE_AIR` → `KR_AIR_REPLENISH` → `OTHER_REGION_FULFILL`)

| 필드 | 형식 | NULL | 뜻 |
| --- | --- | --- | --- |
| `option_type` | text | 아니오 | `KEEP_PLAN`·`PRE_DEPARTURE_AIR`·`KR_AIR_REPLENISH`·`OTHER_REGION_FULFILL` |
| `precheck` | text | 아니오 | `CALCULATE`(T10이 계산) · `NO_SUPPLY`(공급 없음, T10이 "계산 완료 `NOT_FEASIBLE`"로 처리) · `NEEDS_CHECK`(T10이 계산하지 않음, 또는 SKU 항목만). **이 계약에서 새로 정하는 값**이고 DB에 저장하지 않습니다 |
| `reason_code` | text | 예 | `NO_SUPPLY`·`NEEDS_CHECK`일 때의 사유 |
| `supply_available_date` | date | 예 | 공급 가능일 S. 리드타임이 없으면 `null` |
| `eligible_sku_codes[]` | 배열 | — | `KR_AIR_REPLENISH`만 값이 있고 나머지는 빈 배열. 긴급 항공 가능한 K_IMPACTED SKU |

후보 규칙(D-02, `options.candidates`): `KEEP_PLAN`과 `OTHER_REGION_FULFILL`은 항상 들어갑니다. `PRE_DEPARTURE_AIR`는 해상 선적이 `PRE_DEPARTURE`일 때만, `KR_AIR_REPLENISH`는 해상 `AT_SEA`·`AT_PORT` 또는 항공 `PRE_DEPARTURE`·`IN_AIR`일 때만 들어갑니다. 후보가 아니면 행이 없습니다.

사전 판정(`options.evaluate` 114~168행)

| 대응안 | 조건 | `precheck` / `reason_code` | 공급일 S |
| --- | --- | --- | --- |
| `KEEP_PLAN` | 항상 | `CALCULATE` | E |
| `PRE_DEPARTURE_AIR` | 한국 출고 처리·항공·도착 입고 처리 중 하나라도 없음 | `NEEDS_CHECK` / `MISSING_OPERATING_RULE` | `null` |
| | S ≥ E | `NO_SUPPLY` / `ARRIVES_AFTER_DELAYED_SHIPMENT` | max(D, 한국 출고 예정일) + 세 리드타임 |
| | 그 밖 | `CALCULATE` | 같음 |
| `KR_AIR_REPLENISH` | 세 리드타임 중 하나라도 없음 | `NEEDS_CHECK` / `MISSING_OPERATING_RULE` | `null` |
| | S ≥ E | `NO_SUPPLY` / `ARRIVES_AFTER_DELAYED_SHIPMENT` | D + 세 리드타임 |
| | 긴급 항공 가능한 K_IMPACTED SKU가 없거나 모두 한국 보유 ≤ 0 | `NO_SUPPLY` / `NO_KR_ELIGIBLE_STOCK` | 같음 |
| | 그 밖 | `CALCULATE` | 같음 |
| `OTHER_REGION_FULFILL` | 공여 창고 출고 처리·공여 → 도착 권역 배송 중 하나라도 없음 | `NEEDS_CHECK` / `MISSING_OPERATING_RULE` | `null` |
| | K_IMPACTED 전부 공여 창고 다음 입고 없음 | `NEEDS_CHECK` / `DONOR_NO_NEXT_INBOUND` (T10은 SKU 항목만, 집계 `null`) | D |
| | 그 밖 | `CALCULATE` | D |

## 6. T10 `calculate_option_outcomes` — 대응안 결과 (A 구현)

입력: T9 출력과 T1·T2·T3·T4·T5·T8, T6 `items[]`·영향 집합, T7 `risks[]`·`expected_plan_simulation`·`simulation_horizon_end`.

출력: `options[]`(T9와 같은 순서). 한 항목이 `response_options` 1행 + `option_order_outcomes` 여러 행이 되고, 서비스가 그대로 저장합니다(`response_option_id` 등 식별자와 `reason` 열은 서비스가 채움).

| 필드 | 형식 | NULL | 뜻 | 저장 열 |
| --- | --- | --- | --- | --- |
| `option_type` | text | 아니오 | | `option_type` |
| `feasibility_status` | text | 아니오 | `FEASIBLE`·`NOT_FEASIBLE`·`NEEDS_CHECK` (아래 판정 순서) | `feasibility_status` |
| `reason_code` | text | 예 | `FEASIBLE`이면 `null` | `reason_code` |
| `required_quantity` | 정수 | 아니오 | K_IMPACTED의 `total_prepare_quantity` 합(모든 대응안 같음) | `required_quantity` |
| `securable_quantity` | 정수 | 예 | 대응안 공급 합. 주문 결과를 만들지 않은 대응안은 `null` | `securable_quantity` |
| `reduction_committed_total` | 정수 | 예 | K_IMPACTED 확정 감소(원값 내림) 합. `KEEP_PLAN`은 0 | `reduction_committed_total` |
| `expected_delivery_date` | date | 예 | 공급일 S | `expected_delivery_date` |
| `fully_recovered_orders`·`delay_induced_recovered_orders`·`item_secured_blocked_orders`·`not_recovered_orders` | 정수 | 예 | 주문 단위 집계(`pipeline.aggregates`). 주문 결과가 없으면 모두 `null` | 같은 이름 |
| `sku_results` | `sku_results.v1` | 아니오 | 분석 JSON 계약 v3 1절 그대로(T6 `items[]`와 같은 SKU별 효과, `sku_id` 순) | `sku_results` |
| `outcomes[]` | 배열(`order_line_id`) | — | 위험 주문 **상세** 단위(D21). 아래 표 | `option_order_outcomes` |

`outcomes[]` 한 항목(위험 주문 상세 1개)

| 필드 | 형식 | NULL | 뜻 |
| --- | --- | --- | --- |
| `order_line_id` | uuid | 아니오 | 서비스가 `at_risk_order_id`로 바꿔 저장 |
| `outcome` | text | 아니오 | `RECOVERED`·`NOT_RECOVERED`. 주문 전체가 약속일까지 배송되면 그 주문의 모든 상세가 `RECOVERED` |
| `not_recovered_reason` | text | 예 | `NOT_RECOVERED`일 때 사유. 대응안별로 6.1절 표 B의 순서로 처음 맞는 것 |
| `line_securement_status` | text | 아니오 | 확보 수량 ≥ 미출고 수량 `FULLY_SECURED`, > 0 `PARTIALLY_SECURED`, 0 `NOT_SECURED` |
| `secured_quantity` | 정수 | 아니오 | 판정 기준일의 확보 수량 |
| `recovered_quantity` | 정수 | 아니오 | `RECOVERED`면 미출고 수량, 아니면 0 |
| `expected_delivery_at` | 시각 | 예 | `RECOVERED`일 때 배송일 `T23:59:59Z` |
| `source_warehouse_id` | uuid | 예 | 확보 수량 > 0일 때 공급 창고 |
| `supply_source` | text | 예 | 확보 수량 > 0일 때 `EXISTING_STOCK`·`OTHER_REGION_WAREHOUSE`·`KR_AIR`·`PRE_DEPARTURE_AIR` |

`feasibility_status` 판정 순서(`options.evaluate` 169~192행, 위에서부터 처음 맞는 것)

| # | 조건 | 결과 |
| --- | --- | --- |
| 1 | T9 `NEEDS_CHECK` / `MISSING_OPERATING_RULE` | `NEEDS_CHECK`, 집계·`securable_quantity` `null`, `outcomes` 없음 |
| 2 | T9 `NEEDS_CHECK` / `DONOR_NO_NEXT_INBOUND`(전부 판정 불가) | `NEEDS_CHECK` / `DONOR_NO_NEXT_INBOUND`, 집계 `null`, `outcomes` 없음, `sku_results`는 있음 |
| 3 | `KEEP_PLAN` | `FEASIBLE`, `reduction_committed_total` 0, `delay_induced_recovered_orders` 0 |
| 4 | 지연 유발 회복 주문 ≥ 1 또는 확정 감소 합 ≥ 1 | `FEASIBLE` |
| 5 | K_IMPACTED 일부가 공여 창고 다음 입고 없음 | `NEEDS_CHECK` / `DONOR_NO_NEXT_INBOUND` (판정 가능한 범위의 값은 있음) |
| 6 | 확정 감소 원값 합 > 0 (1개 미만) | `NOT_FEASIBLE` / `FORECAST_EFFECT_BELOW_ONE_UNIT` |
| 7 | 다른 권역 출고의 대표 사유(6.1절 C)가 `DONOR_NO_NEXT_INBOUND` | `NEEDS_CHECK` / `DONOR_NO_NEXT_INBOUND` (판정 가능한 범위의 값은 있음, 9절 Q9) |
| 8 | 그 밖 | `NOT_FEASIBLE` / T9의 `NO_SUPPLY` 사유 또는 다른 권역 출고의 대표 사유(6.1절 C), 없으면 `NO_RECOVERY_EFFECT` |

7번은 엔진 수정판(`options.py` `f66e319c6bdd`)의 `evaluate` 189~190행입니다. 수정 전 엔진은 이 경우 `NOT_FEASIBLE` / `DONOR_NO_NEXT_INBOUND`를 냅니다.

엔진에는 2와 3 사이에 "주문 결과가 없으면 `NOT_FEASIBLE`" 단계가 하나 더 있습니다. 1·2가 아닌 대응안은 늘 주문 결과를 만들어서 이 단계에 오지 않으므로 표에서 뺐습니다.

이 표는 집계 NULL 규칙 G-1~G-3과 같은 결과를 냅니다(명세 4.5 "대응안 집계 규칙"). 계산 완료 `NOT_FEASIBLE`(공급 없음 포함)은 집계가 0을 포함한 숫자이고, 위험 주문마다 `NOT_RECOVERED` 결과가 있습니다.

**도구 구현에서 입력과 맞춰 볼 것(모델 하나로는 볼 수 없음):** 엔진 검증기(`bundle_verify.py`)와 같이, `FEASIBLE`·`NOT_FEASIBLE` 대응안의 `outcomes` 행 수 = T7 위험 주문 상세 행 수(`COMPUTED_OUTCOME_ROWS`), 집계가 있는 `NEEDS_CHECK` 아닌 대응안의 `fully_recovered_orders + not_recovered_orders` = 서로 다른 위험 주문 수(`ORDER_COUNT_MISMATCH`). T10 도구가 입력 `at_risk_orders`와 대조합니다(#26 리뷰).

### 6.1 사유 코드 — 대응안별 집합과 고르는 규칙

엔진 `options.py`(`evaluate`·`_no_supply_outcomes`·`_order_reason`·`_other_region`)에서 옮겼습니다. D-02 4.1(데이터 팀 리뷰 문서, 공유 묶음에 없음)의 우선순위는 엔진의 `options.PRIORITY` 목록과 같아서, 아래 표만으로 구현·검증할 수 있게 닫았습니다.

**A. 대응안 사유(`response_options.reason_code`) — 대응안별로 나올 수 있는 값**

| 대응안 | `NEEDS_CHECK` | `NOT_FEASIBLE` |
| --- | --- | --- |
| `KEEP_PLAN` | — | — (항상 `FEASIBLE`) |
| `PRE_DEPARTURE_AIR` | `MISSING_OPERATING_RULE` | `ARRIVES_AFTER_DELAYED_SHIPMENT`(S ≥ E), `FORECAST_EFFECT_BELOW_ONE_UNIT`, `NO_RECOVERY_EFFECT` |
| `KR_AIR_REPLENISH` | `MISSING_OPERATING_RULE` | `ARRIVES_AFTER_DELAYED_SHIPMENT`(S ≥ E), `NO_KR_ELIGIBLE_STOCK`(긴급 항공 대상 없음·한국 보유 ≤ 0), `FORECAST_EFFECT_BELOW_ONE_UNIT`, `NO_RECOVERY_EFFECT` |
| `OTHER_REGION_FULFILL` | `MISSING_OPERATING_RULE`, `DONOR_NO_NEXT_INBOUND` | `FORECAST_EFFECT_BELOW_ONE_UNIT`, 대표 사유(아래 C: `PROMISE_UNREACHABLE_LEADTIME`·`NO_TRANSFERABLE_STOCK`), `NO_RECOVERY_EFFECT` |

- DDL `response_options_ck6`의 `NOT_FEASIBLE` 6종 가운데 `PROMISE_UNREACHABLE_LEADTIME`·`NO_TRANSFERABLE_STOCK`은 **다른 권역 출고의 대표 사유로만** 나옵니다.
- `MISSING_REQUIRED_DATA`(ck7)는 대응안 사유로 쓰지 않습니다(9절 Q7).

**B. 주문 미회복 사유(`option_order_outcomes.not_recovered_reason`) — 위에서부터 처음 맞는 것**

| 대응안 | 순서 | 조건 → 사유 |
| --- | --- | --- |
| `KEEP_PLAN` | 1 | 항상 `NO_RECOVERY_EFFECT` |
| `PRE_DEPARTURE_AIR`·`KR_AIR_REPLENISH` | 1 | 공급일 S + 도착 창고 출고 처리 + 고객 배송 > 약속 배송일 → `PROMISE_UNREACHABLE_LEADTIME` |
| | 2 | (`KR_AIR_REPLENISH`만) 대응안 사유가 `NO_KR_ELIGIBLE_STOCK`이거나, 주문 상세 SKU 중 긴급 항공 불가가 있음 → `NO_KR_ELIGIBLE_STOCK` |
| | 3 | 그 밖 → `NO_RECOVERY_EFFECT` |
| `OTHER_REGION_FULFILL` | 1 | max(접수일, D) + 공여 창고 출고 처리 + 공여 → 도착 권역 배송 > 약속 배송일 → `PROMISE_UNREACHABLE_LEADTIME` |
| | 2 | 주문 상세 SKU 중 공여 창고 다음 입고가 없는 것이 있음 → `DONOR_NO_NEXT_INBOUND` |
| | 3 | 넘길 수 있는 재고가 모자람 → `NO_TRANSFERABLE_STOCK`. 주문은 (약속 시각, 접수일, 주문 코드) 순서로 공여 재고를 차례로 씁니다 |

**C. 다른 권역 출고의 대표 사유 — 판정 순서 7·8번에서 씀**

1. 위험 주문이 하나 이상 있고 **모두 회복되지 않았으면**, 그 주문들의 사유(B) 가운데 우선순위가 가장 높은 것을 고릅니다. 우선순위(엔진 `options.PRIORITY`, D-02 4.1):
   `MISSING_OPERATING_RULE` > `MISSING_REQUIRED_DATA` > `DONOR_NO_NEXT_INBOUND` > `ARRIVES_AFTER_DELAYED_SHIPMENT` > `PROMISE_UNREACHABLE_LEADTIME` > `NO_KR_ELIGIBLE_STOCK` > `NO_TRANSFERABLE_STOCK` > `FORECAST_EFFECT_BELOW_ONE_UNIT` > `NO_RECOVERY_EFFECT`
2. 1이 없고, 공여 창고에서 넘길 수 있는 재고가 SKU마다 모두 0이면 `NO_TRANSFERABLE_STOCK`
3. 그 밖이면 대표 사유 없음 → 판정 순서 8번이 `NO_RECOVERY_EFFECT`

- **대표 사유가 `DONOR_NO_NEXT_INBOUND`인 경우(9절 Q9, 결정):** `NEEDS_CHECK` / `DONOR_NO_NEXT_INBOUND`입니다(판정 순서 7번). 이 사유는 "공여 창고 다음 입고를 몰라 판정할 수 없음"이라 "불가"가 아닙니다. DDL `response_options_ck6`은 이 사유를 `NOT_FEASIBLE`에 허용하지 않고 `ck7`은 `NEEDS_CHECK`에 허용합니다.
  - 생기는 조건: 공여 창고 다음 입고가 없는 SKU가 위험 주문에는 있고 K_IMPACTED에는 없을 때(예: 기존 위험 주문에만 있는 SKU)입니다. K_IMPACTED에 있으면 판정 순서 5번(`NEEDS_CHECK`)으로 먼저 빠집니다.
  - overlay 21건에는 이 경우가 없어 정답표 값은 바뀌지 않습니다.

## 7. T11 `compare_options` — 추천 순위 (A 구현)

입력: T10 `options[]`의 `option_type`, `feasibility_status`, `delay_induced_recovered_orders`, `reduction_committed_total`, `expected_delivery_date`.

출력: `options[]`(T10과 같은 순서)

| 필드 | 형식 | NULL | 뜻 | 저장 열 |
| --- | --- | --- | --- | --- |
| `option_type` | text | 아니오 | | |
| `recommendation_rank` | 정수 | 예 | `FEASIBLE`만 1부터. 그 밖은 `null` | `recommendation_rank` |
| `is_recommended` | boolean | 아니오 | 순위 1 | `is_recommended` |

순위 규칙(B-5, `options._rank`): `KEEP_PLAN` 외 `FEASIBLE` 대응안을 지연 유발 회복 주문 수 내림차순 → 확정 감소 합 내림차순 → 공급일 오름차순 → 고정 순서(출항 전 항공 → 한국 창고 항공 → 다른 권역)로 정렬하고, `KEEP_PLAN`을 맨 뒤에 둡니다. DB가 실행당 추천 1건(`response_options_is_recommended_uq`), 순위 중복 없음(`response_options_rank_uq`), 추천 = 순위 1(`response_options_ck16`), 추천은 `FEASIBLE`만(`response_options_ck4`)을 보장합니다.

T11 출력에는 `feasibility_status`가 없어서 모델은 "순위는 `FEASIBLE`만"(`response_options_ck15`)·"추천은 `FEASIBLE`만"(ck4)을 볼 수 없습니다. 출력에 넣지 않고 **T11 도구 구현이 입력(T10 `options[]`)과 맞춰 봅니다**: `FEASIBLE`이면 순위가 있고 아니면 `null`, `KEEP_PLAN`(늘 `FEASIBLE`)은 맨 뒤 순위, 추천은 순위 1(#26 리뷰).

구현(`tools/option_comparison.py`, GLOW-134)은 입력의 `FEASIBLE`로만 순위를 매겨 위 조건을 지킵니다. 출력은 순위순이 아니라 입력과 같은 고정 순서입니다. `KEEP_PLAN` 외 `FEASIBLE` 대응안의 정렬 값이 `null`이면 멈춥니다(9절 Q15).

비교표(agentflow 4절 "동일 기준 비교표")는 따로 만들지 않습니다. T10 `options[]`와 이 순위를 합친 것이 분석 실행 상세 응답의 `responseOptions`입니다.

## 8. T12 `validate_agent_output` (C 구현, 2차)

입력은 T6·T7·T10·T11 출력과 AI 설명(`explanation.v1`), 출력은 `통과`·`설명 재요청`·`실패`와 오류 목록입니다(agentflow 4절). 설명 검증 규칙은 데이터 팀 설명 스키마와 엔진 `explain`을 기준으로 2차에 정합니다.

## 9. 결정 사항과 남은 질문

Q2~Q9는 PR #19 리뷰에서 설계 담당이 엔진 참조 구현을 기준으로 정했습니다(11-04). Q10~Q12는 모델을 만들며 C가 정했습니다(사용자 결정, 11-04). agentflow와 엔진이 다른 곳은 agentflow를 엔진에 맞추고, agentflow 수정은 설계 담당이 합니다.

| # | 내용 | 상태 | 결정·조치 |
| --- | --- | --- | --- |
| Q1 | 엔진의 배정 모듈 `allocation_core`(규칙 `D00-B-b2-v1`)가 공유 묶음에 없음. 엔진은 `synth2025_v5/allocation_core.py`를 불러옴(`core.py` 12행, `snapshot.py` 18행) | **해소(11-04)** | 데이터 팀이 glowship-plan `838092b` `tools/`에 원본을 추가(SHA-256 `fcb08cf8…`, overlay manifest와 같음). 이 파일로 엔진 자체 시험과 배정 모듈 판정 실행기가 통과함. T5 `allocation_state`·T7 `expected_plan_simulation`의 필드 형식은 다음 PR(엔진 연결)에서 채움 |
| Q2 | T9를 "사전 판정"으로 두고 최종 상태는 T10이 정함 | **결정: 제안대로** | `precheck`(`CALCULATE`·`NO_SUPPLY`·`NEEDS_CHECK`)는 도구 사이에서만 쓰고 DB에 저장하지 않음. 엔진 `options.evaluate` 169~192행. agentflow 4절 T9 설명은 설계 담당이 고침 |
| Q3 | T7을 T6보다 먼저 실행 | **결정: 제안대로** | T7 입력에서 "재고 영향"을 뺌. 엔진 `pipeline.analyze`와 결과가 같음. agentflow 4절·5절 순서도는 설계 담당이 고침 |
| Q4 | 공여 창고 후보가 둘 이상인 고객사 | **결정** | 공여 창고 = 도착 창고가 아닌 `US_3PL` 창고 하나. 미국 창고가 셋 이상인 고객사는 범위 밖이고, 생기면 대응안 규칙부터 다시 정함(2026 H1은 두 고객사 모두 `US_3PL` 2개) |
| Q5 | 리드타임 `day_basis = BUSINESS_DAY` | **결정** | `T14-v1`은 달력일만 계산. `BUSINESS_DAY` 행은 없는 행과 같이 다룸(T8에서 `null`). 그 뒤 처리는 3.6절 규칙 그대로(2026 H1 22행은 모두 `CALENDAR_DAY`) |
| Q6 | 판매 기준 일수 | **결정** | 계산은 28일 고정(`T14-v1`, agentflow·명세 D9). 운영 기준 `sales_basis_days`가 28이 아니면 조용히 무시하지 않고, 입력 검증이 실행을 `ON_HOLD`(`REQUIRED_DATA_INVALID`)로 막음 |
| Q7 | 대응안 사유 `MISSING_REQUIRED_DATA` | **결정** | 대응안 사유로 쓰지 않음. 필수 자료 부족은 실행 전체 `ON_HOLD`(엔진 `snapshot.py`의 `Hold`). DDL `response_options_ck7`은 바꾸지 않음(허용해도 쓰지 않을 뿐) |
| Q8 | `skus.emergency_air_eligible` 형식 | **결정: 제안대로** | boolean. 엔진의 `"true"` 문자열 비교는 CSV를 읽을 때의 형식일 뿐 |
| Q9 | 다른 권역 출고의 대표 사유가 `DONOR_NO_NEXT_INBOUND`가 되는 경우(6.1절 C) | **결정(설계 담당, PR #19 승인 리뷰)** | `NEEDS_CHECK` / `DONOR_NO_NEXT_INBOUND`(판정 순서 7번). 엔진 참조 구현은 설계 담당이 같은 규칙으로 고침(glowship-plan `b80fd04`, `options.py` `f66e319c6bdd`). agentflow 4절 "실행 가능 조건"에도 반영됨(`1c0eb49`). 모델은 `NOT_FEASIBLE`과의 조합을 거부함 |
| Q10 | T6 `min_gap`을 표현할 수 없는 경우 | **결정** | 엔진은 계산할 날이 없으면(D ≥ 입고일) `float("inf")`를 냄(`core.py` 49행). JSON에는 무한대가 없으므로 `null`로 둠. `min_gap`은 가용 − 누적 수요라 음수가 될 수 있어 부호 있는 raw 값으로 받음 |
| Q11 | T6·T7 출력에 없는 저장 열 | **결정** | T6 `available_quantity`·`committed_open_quantity`·`average_daily_sales`·`calculation_status`·`warehouse_id`, T7 `risk_reason`은 T9~T11이 읽지 않아 이 계약에 넣지 않음. 결과 저장(노드·저장 작업)에서 더하고, 그때 이 문서 4절도 함께 고침 |
| Q12 | T5 `allocation_state`·T7 `expected_plan_simulation`의 필드 형식 | **결정** | 엔진 연결 작업(배정기를 실제로 돌리는 작업)에서 정함. 그때까지 모델은 자리만 둠(`list[dict]`·`dict`). Jira GLOW-112 설명의 "2차"(T5·T7·T10 배정 형식)에 있던 일을 옮긴 것. T10은 T7 `expected_plan_simulation`을 입력으로 받는 것 말고는 배정 형식이 없음(출력 `outcomes[]`는 6절에서 정함) |
| Q15 | `KEEP_PLAN` 외 `FEASIBLE` 대응안의 T11 정렬 값이 `null`인 경우 | **결정(A, GLOW-134)** | 자료 부족이 아니라 T10 출력 오류이므로 `ValueError`로 멈춤. 재시도 규칙상 분류 안 된 예외로 `FAILED`가 됨(`docs/agent-flow/agent-retry.md` 2절). 엔진은 `TypeError`로 멈춤 |

### 9.1 모델로 옮기며 채운 것

문서에 형식이 비어 있던 곳은 아래처럼 채웠습니다. 문서 표도 같은 내용으로 고쳤습니다.

| 곳 | 채운 내용 | 근거 |
| --- | --- | --- |
| T6·T7·T10 입력 | "T1·T2…의 출력"을 키 하나에 도구 출력 하나로 묶음: `delay_context`(T1), `inventory_snapshot`(T2), `inbound_schedule`(T3), `sales_history`(T4), `open_orders`(T5), `operating_rules`(T8), `inventory_impact`(T6), `at_risk_orders`(T7), `option_feasibility`(T9). T6은 여기에 `delay_induced_sku_codes`를 더함 | 키 이름 = 도구 이름에서 `get_`·동사를 뺀 것 |
| T2 `kr_on_hand[]` | `{sku_code, on_hand_quantity}` | T2 `items[]`와 같은 이름 |
| T4 `actual_demand[]` | `{warehouse_code, sku_code, demand_date, quantity}` | 엔진 `actual_demand[(창고, SKU, 날짜)]`. 창고는 `demand_totals`처럼 코드로 둠 |
| T9 `eligible_sku_codes[]` | `KR_AIR_REPLENISH`가 아니면 빈 배열 | 1절 "결측은 키를 빼지 않음" |
| T7 시각 비교 | 배송일 → `T23:59:59Z` | 엔진 `storage.py`, 4.1절 |
| 식별자·시각 | 문자열로 받음(소문자 UUID, `…Z` 초 단위) | `uuid.UUID`·`datetime`은 대문자·`+09:00`·마이크로초를 받아 다른 모양으로 내보냄(11-04 확인) |
| 조회 도구 T2~T5·T8의 입력 | **정하지 않음** | 이 문서에 없음. 조회 도구 구현(계산 연결 작업)에서 정함 |

## 10. 작업 중 실패와 원인

| # | 실패 | 원인 | 조치 |
| --- | --- | --- | --- |
| F1 | 처음에는 agentflow 순서(T9 → T10)를 그대로 따라 T9가 최종 실행 가능 여부를 낸다고 보고 계약 범위를 잡음 | agentflow 표만 보고 엔진 `evaluate`의 판정 순서를 확인하지 않음 | 엔진을 읽고 `FEASIBLE`이 계산 결과에 기대는 것을 확인해 5절 제안으로 바꿈 |
| F2 | 처음에는 T9~T11만 정하면 된다고 봄 | 세 도구의 입력이 사실상 분석 입력 스냅샷 전체라는 것을 엔진 `snapshot.build`를 읽기 전까지 몰랐음 | 3·4절에 조회·재고 영향 도구 출력을 함께 정함 |
| F3 | 이 작업의 Jira 위치를 처음에 "EP-7 아래 작업"으로 권함 | 스토리 설명을 읽기 전에 에픽 이름만 보고 정함 | GLOW-19(ST-8 AIR-001 "등록된 도구만 사용")의 하위 작업으로 고침 |
| F4 | 첫 판에서 T6 입력에 T5(열린 주문)를 빠뜨림 | 예측 층이 열린 주문 수량을 미출고 확정 수량으로 쓰는 것(`forecast_sku`의 `commit`)을 표를 쓸 때 놓침 | 다시 읽으며 발견해 4.2절 입력에 추가 |
| F5 | 첫 판에서 T1 출력 필드를 "warehouses.destination"(표 이름.필드) 모양으로 적음 | 출력 필드 이름이 DB 표 `warehouses`와 겹쳐 DB 열처럼 읽힘 | 문서 대조 스크립트가 "DB 열 없음"으로 잡아 `destination_warehouse` 등으로 바꿈 |
| F6 | 조회 도구의 기준을 "입력 확정 시각"으로 정함 | 엔진(CSV 참조 구현)의 `confirm_t`를 그대로 옮기고, serviceflow 5.2절의 순번 기준을 대조하지 않음 | A 리뷰로 1절·3절을 입력 순번(`input_seq`) 기준으로 바꿈 |
| F7 | `NOT_FEASIBLE` 사유 가운데 두 개(`PROMISE_UNREACHABLE_LEADTIME`·`NO_TRANSFERABLE_STOCK`)가 언제 나오는지 적지 않음 | 사전 판정·판정 순서만 옮기고, 주문 사유와 대표 사유를 고르는 함수(`_order_reason`·`_other_region`)를 표로 닫지 않음 | A 리뷰로 6.1절을 추가 |
| F8 | `allocation_core`를 받은 뒤에도 9절 Q1·10절을 "없음"으로 둔 채 push함 | PR 본문만 고치고 문서 본문은 다음 PR로 미룸 | 리뷰 지적으로 이번에 고침 |
| F9 | PR #19가 머지된 뒤에도 9절 Q9를 "새 질문, 오류로 드러냄"으로 둠 | Q9 결정이 마지막 승인 리뷰(머지 직전)에 달렸고, 문서·인수인계에 옮기지 않음 | 모델 작업 전 재검증(리뷰·agentflow·엔진 대조)에서 찾아 이번에 고침 |
| F10 | 10절에 "엔진 자체 시험(50·97·57건)은 배정 시뮬레이션 경로까지 실제 `allocation_core`로 통과"라고 적음 | 시험 건수만 보고, 각 시험이 배정 모듈을 실제로 부르는지 확인하지 않음 | 개발 1·2 세션 일치 시험(11-04)에서 배정 모듈을 빈 파일·망가뜨린 파일로 바꿔 확인. 아래 검증 설명을 고침 |
| F11 | 처음 모델 초안은 `uuid.UUID`·`datetime`·기존 `RawDecimal`(음수 불가)을 그대로 쓰려 함 | 계약 1절 값 모양과 타입의 실제 동작을 대조하지 않음 | 실행해 보고 문자열 타입과 부호 있는 raw 타입으로 바꿈(9.1절) |
| F12 | T6 `items[]`·T10 `sku_results`를 "K_SHIPMENT의 SKU별"로 적음(PR #19). 모델 주석에도 그대로 옮김 | 엔진 `pipeline.analyze`가 K_SHIPMENT 밖 지연 유발 SKU도 예측 행·대응안 항목에 넣는 것(70~72행)을 확인하지 않음. overlay에는 이런 SKU가 없어 데이터로도 드러나지 않음 | 모델 재검증에서 엔진을 다시 읽어 찾음. 문서·주석을 고침. 모델 규칙은 그 SKU를 `IMPACTED`로 받으므로 바뀌지 않음 |
| F13 | 모델이 "`NOT_FEASIBLE`이면 집계는 숫자", "`MISSING_OPERATING_RULE`이면 집계 `null`"(6절 판정 순서 1번·집계 NULL 규칙)을 확인하지 않음 | DDL CHECK(ck8·ck9)만 옮기고, DDL에 없는 계약 규칙을 따로 대조하지 않음 | push 전 Glowship_Plan API 명세 4.5 "대응안 집계 규칙" 표와 대조해 찾음. 모델 규칙 2개와 거부 시험 2건을 더함. overlay 39행은 그대로 통과 |

**문서 검증:**
- **대조 스크립트:** 코드값(백틱 안 대문자 값)은 DDL·엔진 원문에, `표.열`은 DDL 열에, 제약·인덱스 이름은 DDL에, 엔진 키는 엔진 코드에 있는지 대조했습니다. 결과는 0건이고, 새로 정한 `CALCULATE`·`NO_SUPPLY`만 예외로 뒀습니다. 없는 코드값·없는 열·없는 제약을 일부러 넣은 음성 시험 3건은 모두 잡혔습니다.
- **엔진 실행 대조(5~7절):** 엔진 함수를 실제로 실행해 문서 규칙과 대조했습니다. 11-04에 실제 `allocation_core`(`fcb08cf8…`)를 넣고 다시 돌려 **92건 일치**입니다. 대조한 것은 후보 규칙(운송 방식 2 × 단계 5), 추천 순위 4가지 경우, 사전 판정 → 최종 상태 6가지 경우(운영 기준 누락, 지연 선적보다 늦은 공급, 긴급 항공 대상·한국 보유 없음, 공여 창고 다음 입고 전부 없음, 기존 계획 유지)입니다. 문서 규칙을 일부러 세 군데 틀리게 바꾼 음성 실행에서는 어긋남이 잡혔습니다. 이 92건은 수정 전 `options.py`(`974577bcc337`)로 돌린 결과입니다. 수정판이 바꾼 판정 순서 7번(Q9)은 92건에 없는 경우이고, 수정판의 Q9 시험 6건이 따로 확인합니다.
- **6.1절 사유 표:** 엔진 코드에서 옮겼습니다. 그리고 엔진이 실제로 만든 overlay 결과(대응안 39행, 주문 결과 21행)가 표 A·B의 집합 안에 모두 들어가는 것을 확인했습니다. 다른 권역 출고의 `NO_TRANSFERABLE_STOCK` 2행은 위험 주문이 없는 실행이라 규칙 C-2(넘길 재고 전부 0)로 나온 값입니다. 규칙 C-1(모든 주문 미회복 → 우선순위)은 overlay에 사례가 없어 코드로만 확인했습니다.
- **엔진 자체 시험:** 엔진 자체 시험(50·97·57건)은 실제 `allocation_core`로 통과했습니다. 다만 배정 모듈을 실제로 부르는 것은 `test_explain`(97건)뿐입니다(`simulate` 109회, `allocate_day` 3,856회). `test_engine`(50)·`test_forbidden_boundary`(57)는 배정 모듈을 빈 파일이나 망가뜨린 파일로 바꿔도 통과합니다(11-04 개발 1·2 세션 일치 시험, F10). Q9 시험(6건)도 배정 모듈을 씁니다.
- **모델 대조 시험(GLOW-112):** `test_tool_schemas.py`가 이 문서 3~7절 표의 필드 이름과 모델 필드를 양방향으로, 5절·6.1절 표의 사유 집합과 모델 규칙을 대조합니다. 표 A·B와 사전 판정 사유가 DDL `response_options_ck6`·`ck7`, `option_order_outcomes_ck12` 안에 있는지도 따로 대조합니다(문서와 모델을 함께 잘못 고친 경우를 잡기 위해서). 모델 필드를 빼거나 문서에 가짜 필드·코드값을 넣으면 실패합니다. 실제 DB의 overlay 대응안 39행·주문 결과 21행은 T10 모델을, 위험 주문 7행은 T7 모델을 모두 통과합니다.
