# 지연 감지 (할 일 9, GLOW-16)

선적의 일정 스냅샷이 새로 들어올 때 **지연 에피소드를 열고 닫고, 후속 AUTO 분석 실행의 생성 사유를 정하는 규칙**과 구현 범위를 설명합니다.
기준은 serviceflow v6.2 4절 "지연 에피소드와 분석 실행"과 검증 엔진 `04_적재검증도구\glowship_engine\episodes.py`입니다.
판정 순수 함수(1절)와 에피소드 저장(4절)이 있고 SHIPMENT 수집에 연결되어 있습니다. 실행 생성 연결은 5절에 남았습니다.

## 1. 판정 규칙 (`application/detection/episode_rules.py` `decide_episode`)

선적마다 스냅샷을 관측 순서대로 넣습니다. 이번 스냅샷과 직전 스냅샷, 선적의 계획 3PL 입고일(새 선적일 때 한 번 정한 값, serviceflow 1절), 열린 에피소드가 있는지로 판정합니다.

| 경우 | 판정 (`EpisodeAction`) | AUTO 실행 생성 사유 | 종료 사유 |
| --- | --- | --- | --- |
| 열린 에피소드 없음, 입고 전, 예상 3PL 입고일 > 계획 3PL 입고일 | `START` 에피소드 시작 | `EPISODE_STARTED` | — |
| 열림, 지연 일수가 늘어남 | `FOLLOW_UP` | `SCHEDULE_WORSENED` | — |
| 열림, 지연 일수가 줄어듦(아직 계획일 뒤) | `FOLLOW_UP` | `SCHEDULE_IMPROVED` | — |
| 열림, 지연 일수는 같고 단계만 바뀜 | `FOLLOW_UP` | `STAGE_CHANGED` | — |
| 열림, 예상일 ≤ 계획일 | `CLOSE_RECOVERED` | `DELAY_RECOVERED` | `RECOVERED` |
| 열림, 입고 완료(`RECEIVED`) | `CLOSE_RECEIVED` | 실행 없음 | `RECEIVED` |
| 열림, 예상일·단계 모두 같음 / 열리지 않았고 지연 아님 / 이미 입고 완료로 처음 관측 | 없음 (`None`) | — | — |

- 지연 일수 = 예상 3PL 입고일 − 계획 3PL 입고일. 단계와 예상일이 함께 바뀌면 지연 일수 변화를 먼저 봅니다(엔진과 같음).
- 입고 완료는 예상일이 계획일 이내여도 `RECEIVED` 종료입니다(회복 판정보다 먼저).
- 열린 에피소드인데 직전 스냅샷이 없으면 `ValueError`입니다(에피소드를 연 스냅샷이 반드시 있음).

## 2. 확정 데이터 재현

확정 2026 상반기 스냅샷 223건을 선적별 관측 순서대로 넣으면 확정 DB와 같습니다(`test_episode_replay_sql.py`).

| 비교 | 확정 DB | 재현 |
| --- | --- | --- |
| `delay_events` (선적, 에피소드 번호, 감지·직전 스냅샷, 감지·종료 시각, 종료 사유) | 8건 (입고 종료 7, 회복 종료 1) | 8건, 모든 값 같음 |
| AUTO 실행 (선적, 생성 사유, 원인 스냅샷) | 19건 (`EPISODE_STARTED` 8, `STAGE_CHANGED` 8, `SCHEDULE_WORSENED` 2, `DELAY_RECOVERED` 1) | 19건, 모두 같음 |

- 에피소드 감지 시각 = 감지 스냅샷의 `observed_at`, 종료 시각 = 종료 스냅샷의 `observed_at`입니다.

## 3. 정한 것

| 항목 | 결정 | 근거 |
| --- | --- | --- |
| 어디서 부르나 | 수집 적재의 같은 트랜잭션 안, 일정 스냅샷을 남긴 뒤·STALE 판정 전 | 새 AUTO 실행이 생기면서 이전 실행을 `STALE`(`DELAY_SCHEDULE_CHANGED`·`DELAY_STAGE_CHANGED`)로 바꾸는 순서가 맞음 (serviceflow 4절) |
| 운영 중 새로 들어온 선적의 첫 스냅샷이 이미 지연 | 에피소드를 엶 | 결정 1-10은 "초기 적재"의 기준 스냅샷만 지연 이벤트를 만들지 않음. 확정 데이터에는 두 경우를 가르는 선적이 없어 재현 결과는 같음 |
| 같은 스냅샷으로 실행을 두 번 만들지 않음 | 스냅샷 1건에 판정 1번 | serviceflow 4절, DDL `analysis_runs_auto_trigger_uq` |

## 4. 에피소드 저장 (`application/detection/episode_service.py`, `infrastructure/repository/delay_event_write_repository.py`)

`DataIngestService`가 SHIPMENT 수집에서 수집 완료 기록 뒤·STALE 판정 전에 같은 트랜잭션으로 `EpisodeDetectionService.detect_after_sync(company_id, data_sync_run_id)`를 부릅니다(3절 위치). 판정 결과는 수집 결과 `IngestResult.episode_outcomes`에 담깁니다.

| 단계 | 하는 일 |
| --- | --- |
| 초기 적재 확인 | 이 수집 말고 적재된(`SUCCEEDED`·`PARTIAL_SUCCESS`) SHIPMENT 수집이 없으면 초기 적재라 아무것도 하지 않음(아래 "초기 적재 예외") |
| 판정 재료 | 이 수집이 남긴 스냅샷마다 선적의 계획 3PL 입고일, 직전 스냅샷(같은 선적의 다른 스냅샷 중 관측 시각이 가장 늦은 것), 선적의 마지막 에피소드 번호와 열린 에피소드 |
| `START` | `delay_events` 한 행: `episode_no` = 마지막 번호 + 1, `dedupe_key` = `{shipment_id}:EP{n}`(ck6), 감지·직전 스냅샷, 단계·계획·예상 입고일, `detected_at` = 스냅샷 `observed_at` |
| `CLOSE_RECEIVED`·`CLOSE_RECOVERED` | 열린 에피소드의 `closed_at` = 스냅샷 `observed_at`, `close_reason` = `RECEIVED`·`RECOVERED`. 열린 행만 고치므로 한 번만 기록됨 |
| `FOLLOW_UP` | `delay_events`에 쓰지 않음 |
| 돌려주는 값 | 스냅샷마다 판정·지연 ID·원인 스냅샷·AUTO 실행 생성 사유(`CLOSE_RECEIVED`는 `None`). 실행 생성 연결(5절)이 이 값을 씀 |

- 읽기는 직접 SQL, 쓰기는 ORM 엔터티 기반 문장입니다.
- **종료 권한:** 공유 DDL은 워커 역할에 `delay_events` UPDATE를 주지 않습니다(813행). 설계 담당이 2026-11-04에 워커에게 `closed_at`·`close_reason` 두 열만 UPDATE를 주기로 정했고(ERD 설명 4절), 서버 리비전은 개발자 3이 넣습니다. 그 전까지 운영(워커 역할)에서는 종료가 권한 오류이고, 로컬 개발 DB는 슈퍼유저라 동작합니다.
- **초기 적재 예외(결정 1-10) — 이 절이 이 규칙의 기준입니다.**
  - **무엇이 초기 적재인가:** 고객사의 **첫 SHIPMENT 수집**(그 전에 적용된 `SHIPMENT` 수집 `SUCCEEDED`·`PARTIAL_SUCCESS`가 없음)입니다. 그 수집이 남긴 일정 스냅샷이 결정 1-10의 기준 스냅샷입니다.
  - **누가 가르나:** 이 저장 단계가 가릅니다(`has_prior_applied_shipment_sync`). 초기 적재 수집의 스냅샷은 `decide_episode`에 넣지 않습니다. 그 뒤 수집부터는 이 스냅샷을 직전 스냅샷(`previous`)으로 씁니다.
  - **`decide_episode`는 이 신호를 받지 않습니다.** 순수 판정이라 받은 스냅샷만 봅니다. `previous is None`으로는 가를 수 없습니다(운영 중 새로 들어온 선적의 첫 스냅샷도 직전 스냅샷이 없음, 3절에 따라 에피소드를 엶).
  - 확정 데이터: 두 고객사의 첫 SHIPMENT 수집(2025-12-31)이 남긴 스냅샷이 각 5건이고, 2026 이전 스냅샷 10건과 정확히 같습니다. 그중 이미 지연인 스냅샷은 없어 223건 재현으로는 이 규칙이 검증되지 않으므로, 별도 테스트(`test_ingest_episode_sql.py`: 첫 수집의 지연 스냅샷 → 에피소드 없음, 다음 수집의 지연 → 시작)로 확인합니다.

## 5. 아직 없는 것

- **실행 생성 연결:** `START`·`FOLLOW_UP`·`CLOSE_RECOVERED`일 때 `create_analysis_run`(개발자 3)을 불러 AUTO 실행을 만들고(원인 스냅샷 필수, ck16), 선택 전 이전 실행을 `STALE`로 바꿉니다. `CLOSE_RECOVERED`의 실행은 `NO_DELAY`입니다.

## 6. 테스트

| 파일 | 내용 |
| --- | --- |
| `test_episode_rules.py` | 1절 표의 경우마다 하나씩, 입고 완료가 회복보다 먼저, 직전 스냅샷 없음 오류 (DB 없음) |
| `test_episode_replay_sql.py` | 확정 스냅샷 223건 재현 → `delay_events` 8건·AUTO 실행 19건과 같음 (실제 DB) |
| `test_episode_service.py` | 초기 적재 건너뜀, 시작(다음 번호·관측 시각), 운영 중 첫 관측 지연 → 1번, 입고·회복 종료, 후속은 저장 안 함 (DB 없음) |
| `test_ingest_episode_sql.py` (data_sync) | 수집 → 지연 선적 에피소드 시작·입고 종료, 초기 적재의 지연 스냅샷은 에피소드 없음·다음 수집에서 시작, 다른 원천은 보지 않음 (실제 DB) |
| `test_ingest_replay_sql.py` (data_sync) | 확정 스냅샷을 수집으로 다시 넣으면 재현 선적의 `delay_events`가 원래와 같고(8건), 판정 사유가 확정 AUTO 실행 19건과 같음 (실제 DB) |
| `test_delay_event_write_repository_sql.py` | 첫 SHIPMENT 수집만 초기 적재(롤백 트랜잭션), 이번 수집 스냅샷·직전 스냅샷, 선적별 번호·열린 에피소드, 추가·한 번만 종료와 DB 제약 (실제 DB) |
