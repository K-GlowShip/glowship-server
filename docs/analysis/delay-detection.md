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

`DataIngestService`가 SHIPMENT 수집에서 수집 완료 기록 뒤·STALE 판정 전에 같은 트랜잭션으로 부릅니다(3절 위치). **판정과 종료를 나눕니다.**

```
detect_after_sync (판정, 시작만 저장)
  → create_after_sync (GLOW-18, 5절: AUTO 실행 생성 · 후속·회복이면 이전 실행 STALE)
  → close_episodes (입고·회복 종료)
  → judge_after_sync (STALE 판정, 닫힌 에피소드의 실행은 대상 아님)
```

- `mark_runs_stale`은 닫힌 에피소드의 실행을 동결로 보고 건너뜁니다(`stale_service._unfrozen`). 그래서 회복 종료 전에 이전 실행을 STALE로 바꿀 수 있게 종료를 따로 둡니다. 확정 데이터도 회복된 지연의 v1이 `DELAY_SCHEDULE_CHANGED`로 STALE이고 v2가 `NO_DELAY`입니다.
- **입고 종료는 이전 실행을 STALE로 바꾸지 않습니다**(설계 결정 2026-11-04). serviceflow 4절은 "에피소드 종료(`RECEIVED`), 실행을 만들지 않음"이고, 확정 데이터에서도 입고로 닫힌 지연의 마지막 실행은 그대로입니다(예: 01-22 입고, v4 `NO_RISK`).
- 판정 결과는 수집 결과 `IngestResult.episode_outcomes`에 담깁니다.

| 단계 | 하는 일 |
| --- | --- |
| 초기 적재 확인 | 이 수집 말고 선적을 1건 이상 적재한(`SUCCEEDED`·`PARTIAL_SUCCESS`, `loaded_record_count > 0`) SHIPMENT 수집이 없으면 초기 적재라 아무것도 하지 않음(아래 "초기 적재 예외") |
| 판정 재료 | 이 수집이 남긴 스냅샷마다 선적의 계획 3PL 입고일, 직전 스냅샷(같은 선적의 스냅샷 중 그 스냅샷을 남긴 수집의 적용 순번이 이번 수집보다 작은 것 가운데 가장 큰 것. 관측 시각 순이 아님: 이슈 #39 전 동시 수집은 관측 시각과 적용 순서가 뒤집힐 수 있었음, 이슈 #34), 선적의 마지막 에피소드 번호와 열린 에피소드 |
| `START` | `delay_events` 한 행: `episode_no` = 마지막 번호 + 1, `dedupe_key` = `{shipment_id}:EP{n}`(ck6), 감지·직전 스냅샷, 단계·계획·예상 입고일, `detected_at` = 스냅샷 `observed_at` |
| `CLOSE_RECEIVED`·`CLOSE_RECOVERED` | 판정에서는 저장하지 않고 결과로 돌려줌. `close_episodes`가 열린 에피소드의 `closed_at` = 스냅샷 `observed_at`, `close_reason` = `RECEIVED`·`RECOVERED`를 기록. 열린 행만 고치므로 한 번만 기록됨 |
| `FOLLOW_UP` | `delay_events`에 쓰지 않음 |
| 돌려주는 값 | 스냅샷마다 판정·지연 ID·원인 스냅샷·관측 시각·AUTO 실행 생성 사유(`CLOSE_RECEIVED`는 `None`)·종료 사유. 실행 생성 연결(5절)과 `close_episodes`가 이 값을 씀 |

- 데이터 접근 방식(`docs/backend/data-access.md`): 읽기는 `LATERAL`·`FILTER`를 쓰는 직접 SQL, 에피소드 한 행 추가는 ORM 엔터티(`db.add` + `flush`), 종료는 열린 행만 고치는 조건부 `update(DelayEvent)` 식입니다.
- **종료 권한:** 공유 DDL은 워커 역할에 `delay_events` UPDATE를 주지 않습니다(813행). 설계 담당이 2026-11-04에 워커에게 `closed_at`·`close_reason` 두 열만 UPDATE를 주기로 정했고(ERD 설명 4절), 마이그레이션 0007(GLOW-117, PR #27)이 그 두 열을 줍니다.
- **초기 적재 예외(결정 1-10) — 이 절이 이 규칙의 기준입니다.**
  - **무엇이 초기 적재인가:** 고객사의 **선적을 처음 적재한 SHIPMENT 수집**(그 전에 선적을 1건 이상 적재한 `SHIPMENT` 수집 `SUCCEEDED`·`PARTIAL_SUCCESS`가 없음)입니다. 그 수집이 남긴 일정 스냅샷이 결정 1-10의 기준 스냅샷입니다.
  - **0건 수집은 세지 않습니다(`loaded_record_count > 0`).** 연결 시험이나 원천 준비 전 호출로 첫 수집이 선적 0건으로 성공하면 기준 스냅샷이 남지 않습니다. 이 수집을 앞선 수집으로 치면 그다음 수집(실제 초기 데이터)의 지연 선적마다 에피소드가 열리고, GLOW-18 뒤에는 AUTO 실행까지 생깁니다. 결정 1-10이 막으려던 경우라 선적을 실제로 적재한 수집만 셉니다(PR #24 리뷰, 2026-11-05). 확정 데이터의 첫 SHIPMENT 수집은 두 고객사 모두 선적을 적재했으므로(A_BEAUTY 59건) 결과는 같습니다.
  - **누가 가르나:** 이 저장 단계가 가릅니다(`has_prior_applied_shipment_sync`). 초기 적재 수집의 스냅샷은 `decide_episode`에 넣지 않습니다. 그 뒤 수집부터는 이 스냅샷을 직전 스냅샷(`previous`)으로 씁니다.
  - **`decide_episode`는 이 신호를 받지 않습니다.** 순수 판정이라 받은 스냅샷만 봅니다. `previous is None`으로는 가를 수 없습니다(운영 중 새로 들어온 선적의 첫 스냅샷도 직전 스냅샷이 없음, 3절에 따라 에피소드를 엶).
  - 확정 데이터: 두 고객사의 첫 SHIPMENT 수집(2025-12-31)이 남긴 스냅샷이 각 5건이고, 2026 이전 스냅샷 10건과 정확히 같습니다. 그중 이미 지연인 스냅샷은 없어 223건 재현으로는 이 규칙이 검증되지 않으므로, 별도 테스트(`test_ingest_episode_sql.py`: 첫 수집의 지연 스냅샷 → 에피소드 없음, 다음 수집의 지연 → 시작)로 확인합니다.

## 5. AUTO 실행 생성 (`application/run_creation/auto_run_service.py`, GLOW-18, 이슈 #34)

수집 서비스가 `detect_after_sync`와 `close_episodes` 사이에서 `AutoRunService.create_after_sync`를 부릅니다(같은 L1·같은 트랜잭션).

| 판정 | 하는 일 |
| --- | --- |
| `START` | `create_analysis_run`(AUTO, `EPISODE_STARTED`) → v1 |
| `FOLLOW_UP` | 이전 실행(현재 실행)을 `mark_runs_stale`로 STALE → 새 버전(악화·개선·단계 변경 사유) |
| `CLOSE_RECOVERED` | 이전 실행 STALE → `DELAY_RECOVERED` 실행(분석이 `NO_DELAY`로 끝냄) → 그 뒤 `close_episodes`가 닫음 |
| `CLOSE_RECEIVED` | 실행 없음, 이전 실행 그대로 |

- **이전 실행 STALE 사유:** 단계 변경(`STAGE_CHANGED`)이면 `DELAY_STAGE_CHANGED`, 그 밖은 `DELAY_SCHEDULE_CHANGED`(엔진 하네스 `run_s1_s5.py` `new_run`, 확정 DB 와 같음). 감지 순번 = 이 수집의 적용 순번, 감지 시각 = 수집 완료 시각.
- **새 실행:** 원인 스냅샷 = 판정한 스냅샷(ck16), **실행 시각 = 이 수집의 확정 시작 시각**(= 고객사 마지막 적재 수집 시작, 이슈 #50), 데이터 버전 기록, 상태 `DETECTED`. 같은 지연·같은 원인 스냅샷의 AUTO 실행은 `create_analysis_run`이 한 건만 만듭니다. 만든 실행 ID 는 `IngestResult.created_run_ids`에 담깁니다.
- **분석 보류(GLOW-15)는 여기서 판정하지 않습니다**(사용자 결정 11-06). 분석을 시작할 때 엔진 입력(#53 `EngineHold`)이 같은 "차단 검증 오류" 조건으로 정합니다. 분석 시작(`DETECTED` → 분석)은 GLOW-128(#51)입니다.
- **실행 생성 실패는 수집을 막지 않습니다(#63 리뷰 [권장], 2026-11-09 결정 1안).** serviceflow 5.2절처럼 입력 확정(실행 생성)과 수집 적재를 다른 단계로 봅니다.
  - 판정 하나마다 SAVEPOINT(`begin_nested`)로 감쌉니다. 데이터·운영 조건 오류(`SKIPPABLE`: `OPERATING_RULE_MISSING` 그날 운영 기준 없음, `RUN_CODE_EXHAUSTED` 그날 실행 999건 소진, `EPISODE_START_MISMATCH`)면 그 판정만 되돌리고(이전 실행 STALE 포함) 경고 로그를 남긴 뒤 다음 판정과 수집을 계속합니다. 건너뛴 판정은 `IngestResult.skipped_auto_runs`(지연 ID·오류 코드·메시지)에 담깁니다. 선적·스냅샷·에피소드는 그대로 적재됩니다.
  - 그 밖의 오류(`INVALID_TRIGGER`·`DELAY_EVENT_NOT_FOUND`·`SNAPSHOT_NOT_OF_SHIPMENT` 등)는 판정과 실행 생성이 어긋난 코드 오류라 지금처럼 수집을 `FAILED`로 끝냅니다.
  - **실행이 없는 열린 지연:** 시작 때 실행을 만들지 못했거나(위) 이 기능 전에 열린 지연입니다. 다음 후속 판정(악화·개선·단계 변경)에서 **첫 실행(v1)을 `EPISODE_STARTED`로** 만듭니다(첫 실행은 이 사유로만, ck18). 실행 없이 회복으로 닫히면 실행을 만들지 않습니다(분석할 지연이 없음). 입고 종료는 원래 실행이 없습니다.
  - 운영 기준이 없어 건너뛴 고객사는 기준을 넣은 뒤의 후속 판정에서 실행이 생깁니다(재분석 API 20 은 실행 ID 가 있어야 하므로 실행이 없는 지연에는 쓸 수 없음). 분석 결과의 운영 기준 부족은 대응안 "확인 필요"(`MISSING_OPERATING_RULE`, serviceflow 280줄)로 다룹니다(실행 생성 단계와 별개).
  - **닫힌 지연에 남은 시작 전 실행(이슈 #51, 위 "실행 없는 열린 지연"과 다른 경우):** 입고 종료는 실행을 만들지 않고, 운영 기준이 없는 날의 회복은 회복 실행 생성을 건너뛴 채 지연을 닫습니다. 그 전 실행이 `DETECTED`였으면 닫힌 지연에 남습니다. 분석 자동 시작의 시작 정리가 이 실행을 시작하지 않고 `STALE`로 끝냅니다(`auto-start.md` 5절). 실행 생성 쪽은 바꾸지 않았습니다.
  - **결정(2026-11-10, 이슈 #102): 그대로 둡니다.** 후속 판정(악화·개선·단계 변경)이 오지 않으면 실행 없이 남습니다. 그동안 지연 목록에는 보이지만 `currentRun`이 없어 분석할 길이 없습니다. 판정이 없어도 다음 수집에서 v1 을 만드는 방식은 수집 흐름을 바꾸는 일이라 하지 않았습니다. 건너뛴 판정(`skipped_auto_runs`)을 화면에 보여 줄지는 운영 기준이 없는 고객사가 실제로 생길 때 정합니다(데모 데이터에는 0건).
- 지난 날짜를 다시 넣는 재현 시험(`test_ingest_replay_sql.py`)은 실행 시각 확인(`RUN_TIME_BEFORE_INPUT`)에 걸려 실행 생성을 끄는 대역을 씁니다. 실행 생성은 `test_auto_run_sql.py`가 봅니다.

## 6. 테스트

| 파일 | 내용 |
| --- | --- |
| `test_episode_rules.py` | 1절 표의 경우마다 하나씩, 입고 완료가 회복보다 먼저, 직전 스냅샷 없음 오류 (DB 없음) |
| `test_episode_replay_sql.py` | 확정 스냅샷 223건 재현 → `delay_events` 8건·AUTO 실행 19건과 같음 (실제 DB) |
| `test_episode_service.py` | 초기 적재 건너뜀, 시작(다음 번호·관측 시각), 운영 중 첫 관측 지연 → 1번, 판정은 닫지 않고 `close_episodes`가 입고·회복 종료, 후속은 저장 안 함 (DB 없음) |
| `test_ingest_episode_sql.py` (data_sync) | 수집 → 지연 선적 에피소드 시작·입고 종료, 초기 적재의 지연 스냅샷은 에피소드 없음·다음 수집에서 시작, 다른 원천은 보지 않음, 판정 → AUTO 실행 생성 → 종료 → STALE 판정 순서 (실제 DB) |
| `test_ingest_replay_sql.py` (data_sync) | 확정 스냅샷을 수집으로 다시 넣으면 재현 선적의 `delay_events`가 원래와 같고(8건), 판정 사유가 확정 AUTO 실행 19건과 같음 (실제 DB, 실행 생성은 끔) |
| `test_auto_run_sql.py` | AUTO 실행: 새 지연 v1(실행 시각·입력 순번·원인 스냅샷·데이터 버전), 악화·단계 변경 → 이전 실행 STALE 사유·감지 순번 + v2, 회복 → `DELAY_RECOVERED` 뒤 닫힘, 입고 → 없음, 재수집 → 그대로, 사유 대응표, 실행 없는 열린 지연의 후속 → v1 `EPISODE_STARTED`·회복 → 실행 없음, 운영 기준 없음 → 실행만 건너뛰고 수집 적재, 건너뛴 판정의 이전 실행 STALE 되돌림 (실제 DB) |
| `test_delay_event_write_repository_sql.py` | 첫 SHIPMENT 수집만 초기 적재(롤백 트랜잭션), 이번 수집 스냅샷·직전 스냅샷(관측 시각이 뒤집혀도 적용 순번 순), 선적별 번호·열린 에피소드, 추가·한 번만 종료와 DB 제약 (실제 DB) |
