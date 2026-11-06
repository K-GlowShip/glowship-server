# 실행 하나의 엔진 입력과 조회 도구 T1~T5·T8

- 대상: `app/modules/delay_analysis/infrastructure/engine/engine_input.py`(엔진 입력), `app/modules/delay_analysis/application/agent/tools/lookup_tools.py`(조회 도구), 시험 `tests/modules/delay_analysis/test_engine_input.py`
- 티켓: GLOW-125 2부 (상위 GLOW-20). 1부는 엔진 입력 저장소(`engine-input-store.md`, PR #36)
- 계약: `docs/agent-flow/tool-contracts.md` 3절(조회 도구 출력), 도구 호출 인터페이스(개발 2 GLOW-129 `agent-tool-interface.md`) 결정 1~3
- 쓰는 곳: 그래프 준비 노드(GLOW-129)가 실행마다 한 번 만들어 `ToolContext.engine`에 넣고, 도구 T1~T8·설명 생성이 읽습니다

## 1. 왜 이렇게 나눴나
- 엔진은 `pipeline.analyze(snap)` **한 번**으로 예측 층·주문 층·영향 집합·결과 분류·대응안·판정 서명·의존 범위를 함께 계산합니다(`pipeline.py` 64~79행). 판정 서명에는 대응안 결과까지 들어갑니다.
- 도구마다 따로 계산하면 도구 출력과 판정 서명이 다른 계산에서 나올 수 있습니다. 그래서 **실행마다 엔진 입력(스냅샷 + 계산 결과)을 한 번 만들고, 도구는 거기서 옮기기만** 합니다.
- 도구는 DB를 다시 읽지 않습니다. 입력 순번 뒤에 바뀐 값이 섞이지 않고, 몇 번 불러도 같은 값입니다.

## 2. 흐름 (엔진 하네스 `run_s1_s5.analyze_run`과 같은 순서)
| 단계 | 함수 | 결과 |
| --- | --- | --- |
| 1. NO_DELAY 판정 | `judge_no_delay(session, run)` | E ≤ P면 `NoDelay`(판정 서명·SHA-256) → `save_no_delay`. 저장소를 읽지 않습니다(가벼운 조회 두 개) |
| 2. 엔진 입력 | `build_engine_input(session, run)` | 저장소(#36) → `snapshot.build` → `pipeline.analyze`. 입력 검증 실패(엔진 `Hold`)는 `EngineHold(code, message)`로 돌려줍니다(예외 아님) → 실행 ON_HOLD(VALIDATING) |
| 3. 도구 | `lookup_tools.get_*`(T1~T5·T8) | `ctx.engine.snap`을 계약 출력 모델로 옮김 |
| 4. 판정 서명 | `decision_of(engine)` | `EngineDecision`(상태·결과 분류·`dependency_scope`·`decision_signature`·`decision_signature_sha256`). 결과 저장(#38)의 같은 이름 칸에 그대로 넣습니다. 복사본을 돌려줍니다 |

- 저장소·입력 순번이 맞지 않거나 실행의 지연 이벤트·트리거 스냅샷을 못 찾으면 `EngineInputError`·`LookupError`를 올립니다. 이것은 보류가 아니라 시스템 문제입니다. 지연 이벤트를 못 찾는 흔한 원인은 RLS입니다(앱 역할 세션, ACTIVE가 아닌 고객사). 그래서 오류 메시지가 저장소(`_check_not_silently_empty`)와 같이 두 가지를 확인하라고 안내합니다.

## 3. 실행 행에서 엔진 입력 값을 정하는 규칙 (#36 정답 대조와 같음)
| 엔진 값 | 정하는 법 |
| --- | --- |
| 입력 확정 시각 `confirm_t` | `engine_timestamp(run.started_at)`(UTC, 초 아래 버림) |
| 기준일 D | `confirm_t`의 날짜 |
| 지연 선적 | `delay_events.shipment_id` |
| 단계·예상 입고일 E | 실행의 트리거 일정 스냅샷(`trigger_shipment_schedule_snapshot_id`). **트리거가 없으면(재분석)** 엔진처럼 입력 확정 시각까지의 최신 관측(`store.snapshot_at`). 계약 3.1절·9절 Q16과 같음 |
| 계획 입고일 P | `shipments.planned_3pl_inbound_date` |
| 행사 일정 | 엔진 폴더 `glowship_engine/config/event_calendar_2026h1_v1.json`(하네스 `run_s1_s5.CAL`과 같은 파일, 해시는 엔진 매니페스트가 고정) |

- **전제(11-06, #36 리뷰 후속):** `run.started_at`은 **수집과 같은 시각 기준**이어야 합니다. #40으로 수집 시각은 가상 시각(기본 2026-07-02부터 하루씩)이 됐는데, 실행 생성(GLOW-99 `create_analysis_run`)은 아직 `clock_timestamp()`(실제 시각)입니다. 실제 시각으로 만든 실행을 넘기면 D가 11월이 되어 28일 ORDER 수집이 없다며 **모든 실행이 보류**됩니다(재현: A_BEAUTY 실행을 `2026-11-06T01:43Z`로 넘기면 `REQUIRED_DATA_INVALID "ORDER 수집 결측일 [2026-10-09 …]"`, 마지막 수집 시각 + 1분으로 넘기면 `NO_RISK`). 엔진 하네스는 AUTO 실행을 수집한 날 `00:05Z`, 재분석을 사용자 요청 시각으로 만들어 늘 수집과 같은 날입니다. 고치는 곳은 실행 생성·자동 시작(개발 2)이고, 이 문서의 규칙(`started_at` → D)은 serviceflow 337행과 같아 그대로 둡니다. 기록: `tools/doc-deviations` DV1-9(개발 세션 작업 폴더).
- NO_DELAY 판정은 저장소 없이 SQL로 E를 읽습니다. 트리거가 없을 때의 "최신 관측"은 엔진 `snapshot_at`과 같게 고릅니다: 입력 순번까지 적재된 관측 중 `observed_at`(초 아래 버림) ≤ `confirm_t`인 마지막 것, 같은 초면 읽은 순서(`ctid`)의 마지막.

## 4. 정한 것 (11-06)
| # | 내용 | 결정 |
| --- | --- | --- |
| 1 | 엔진 계산 시점 | **만들 때 스레드에서 한 번 계산**(사용자 결정). 합의 초안은 "처음 필요할 때"였지만, 동기 속성으로 나중에 계산하면 이벤트 루프가 그동안 멈춥니다(PR #42 리뷰와 같은 문제). 보류가 아닌 실행은 어차피 T6·T7·판정 서명에 계산 결과가 필요합니다. 속성 이름 `analysis`는 그대로입니다 |
| 2 | T8 리드타임 `day_basis = BUSINESS_DAY` 행 | **계약대로 `null`**(사용자 결정). 엔진은 `day_basis`를 보지 않고 그 일수로 계산하므로 그런 행이 생기면 T8과 계산이 어긋납니다(2026 H1 22행은 모두 `CALENDAR_DAY`). 멈추는 쪽으로 바꾸는 안은 개발 1 개선안 제안으로 남겼고, 처리는 입력 검증(GLOW-100)에서 다시 정합니다 |
| 3 | T5 `allocation_state` 형식(계약 9절 Q12) | 엔진 주문 층이 읽는 재생 결과를 엔진 모양 그대로: 확보 뒤 출고 대기 주문(`pending_dispatch`), 대기 주문 코드, SKU별 보호·확보 수량, 재생 해시. 계약 3.5절 목록 |
| 4 | T4 28일 합계 | 스냅샷의 `mu_ch`는 합계 ÷ 28 실수라 정수 합계를 되돌릴 수 없습니다. 엔진 Store의 같은 색인(`demand_ch`)에서 정수로 셉니다. 시험이 합계 ÷ 28 = `mu_ch`를 확인합니다 |
| 5 | T8 리드타임을 고른 행 | 엔진과 같은 규칙으로 다시 고릅니다(`lead_days`는 첫 행, 고객 배송은 반복의 마지막 행). 고른 행의 일수가 엔진 값과 다르면 멈춥니다(고르는 규칙이 엔진과 어긋난 것) |
| 6 | 도구의 실행 문맥 타입 | 개발 2 `ToolContext`(아직 develop 전)를 import하지 않고 `engine`만 있는 Protocol로 받습니다. 실행 때 `ctx.engine`은 이 파일의 `EngineInput`입니다 |

## 5. 시험·검증
| 구분 | 내용 | 결과(11-06) |
| --- | --- | --- |
| 단위(엔진·DB 없음) | 손으로 만든 스냅샷: 정렬 키, 실행을 못 찾을 때 안내 문구, T1 입력 확인, T4 28일 창·Store를 건드리지 않음, T5 형식, T8 리드타임·`BUSINESS_DAY` `null`·빈 행·고른 행 불일치·불리언, 모르는 단계 코드, 스냅샷 불변·같은 값, `decision_of` 복사, 엔진에 트리거 단계·예상일을 넘김, NO_DELAY가 트리거 E를 씀 | 14건 통과 |
| DB(엔진 경로 + overlay) | B NO_DELAY·ON_HOLD, A READY, A 재분석(트리거 없음). 저장된 판정 서명·SHA·의존 범위·보류 코드와 같은지, 도구 교차 확인(T1 트리거 = 실행 값, T8 운영 기준 = 실행 값, T4 ÷ 28 = `mu_ch`, **T8 SKU ⊇ 영향·관찰·선적 SKU**, T5 ⊇ 의존 범위의 약속 주문 줄, T3 ⊇ 의존 범위의 입고 선적) | 3건 통과(약 21초) |
| 정답 대조 `GLOWSHIP_ENGINE_GOLDEN=1` | overlay 실행 21건(A 13·B 8) 모두 위 대조 | 통과(2분 18초) |
| 변이 | D 하루 밀기 → 2건 실패 / 재분석 최신 관측을 가장 이른 것으로 → 1건 / T4 창 하루 밀기 → 3건 / T8 `BUSINESS_DAY`도 값 그대로 → 1건 / T5 공여 창고 주문 빼기 → 3건 / 트리거 단계·예상일 무시 → 1건(아래 6절) | 모두 잡힘 |

## 6. 작업 중 실패와 원인
- **트리거 값을 엔진에 넘기지 않아도 정답 대조 21건이 통과했습니다.** overlay 데이터는 모든 AUTO 실행의 트리거 스냅샷이 입력 확정 시각의 최신 관측과 같아서, 엔진이 스스로 고른 값과 차이가 나지 않습니다. 데이터 대조로는 이 규칙을 확인할 수 없음을 변이 시험으로 알았습니다 → 엔진 모듈을 대역으로 바꿔 엔진에 넘기는 값을 직접 보는 시험을 더했습니다.
- 계약 문서 3절에 `UNEXPECTED_TOOL_ERROR`를 백틱으로 적었다가 계약 시험(`test_doc_codes_are_known`)이 "모르는 코드"로 잡았습니다. 계약 문서의 백틱 대문자 값은 서버 열거형(`analysis_enums`, DDL CHECK 값)·사전 판정 값·시험의 예외 목록과 대조됩니다 → 실패 코드 이름을 빼고 문장으로 적었습니다.
- 3.5절 `allocation_state` 형식을 표로 적으면 계약 시험이 표의 필드를 모델과 대조합니다(모델은 아직 `list[dict]`). 그래서 목록으로 적었습니다.
- 단위 시험의 가짜 주문 ID를 `o1`처럼 적었다가 출력 모델(소문자 UUID만 받음)이 거부했습니다. 모델이 맞게 막은 것이라 시험 값을 UUID로 고쳤습니다.

## 7. 남은 일
- 엔진 입력 캐시(저장소 읽기 고객사당 약 9.5초, 열쇠 `(고객사, input_seq, max(applied_seq))`) — 개발 1 줄의 다음 작업
- 입력 검증(GLOW-100): 운영 기준 `sales_basis_days ≠ 28`, `BUSINESS_DAY` 리드타임 처리, ON_HOLD 저장 연결
- T6·T7(GLOW-126)은 `analysis`의 `fc`·`ol`·`k_imp`·`k_watch`에서 옮깁니다
