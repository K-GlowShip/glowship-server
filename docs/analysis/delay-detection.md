# 지연 감지 (할 일 9, GLOW-16)

선적의 일정 스냅샷이 새로 들어올 때 **지연 에피소드를 열고 닫고, 후속 AUTO 분석 실행의 생성 사유를 정하는 규칙**과 구현 범위를 설명합니다.
기준은 serviceflow v6.2 4절 "지연 에피소드와 분석 실행"과 검증 엔진 `04_적재검증도구\glowship_engine\episodes.py`입니다.
지금은 판정 순수 함수까지 있고, 에피소드 저장과 실행 생성 연결은 0005·`create_analysis_run` 뒤에 붙입니다(4절).

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

## 4. 아직 없는 것

- **에피소드 저장:** `delay_events` 행 추가(`episode_no` = 선적의 다음 번호, `dedupe_key = {shipment_id}:EP{n}`, ck6·uk3)와 종료(`closed_at`·`close_reason`). 0005(PR #16)가 `episode_no`를 넣습니다.
  - **워커 역할에 `delay_events` UPDATE 권한이 없습니다**(공유 DDL 813행, 0005도 범위 밖). 종료를 기록하려면 권한을 어디에 넣을지 정해야 합니다.
- **실행 생성 연결:** `START`·`FOLLOW_UP`·`CLOSE_RECOVERED`일 때 `create_analysis_run`(개발자 3)을 불러 AUTO 실행을 만들고(원인 스냅샷 필수, ck16), 선택 전 이전 실행을 `STALE`로 바꿉니다. `CLOSE_RECOVERED`의 실행은 `NO_DELAY`입니다.
- **초기 적재 예외(결정 1-10):** 초기 적재의 기준 스냅샷으로는 에피소드를 열지 않는 규칙을 저장 단계에서 구분합니다.

## 5. 테스트

| 파일 | 내용 |
| --- | --- |
| `test_episode_rules.py` | 1절 표의 경우마다 하나씩, 입고 완료가 회복보다 먼저, 직전 스냅샷 없음 오류 (DB 없음) |
| `test_episode_replay_sql.py` | 확정 스냅샷 223건 재현 → `delay_events` 8건·AUTO 실행 19건과 같음 (실제 DB) |
