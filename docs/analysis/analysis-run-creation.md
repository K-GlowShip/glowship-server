# 분석 실행 생성 (입력 확정)과 표시용 실행 ID

- 대상: `create_analysis_run`, `record_input_data_versions`(`app/modules/delay_analysis/application/run_creation/run_creation_service.py`), 마이그레이션 `0006`(`analysis_runs.run_code`)
- Jira: GLOW-99(분석 실행 생성·중복 방지), GLOW-113(0006 `run_code`, 명세 D14)
- 근거:
  - 명세 v6.2: 3절(실행이 생기는 세 경로), 8절 "분석 실행 생성", D14
  - serviceflow v6.2 5.2 처리 단계 1(입력 확정)·4(재분석)
  - PR #11 리뷰 합의(시그니처, 호출하는 쪽이 L1)
  - 데이터 팀 엔진 `snapshot.build`·`store.input_seq`·`storage.rows_for`

## 1. 부르는 법 (B 담당 코드용)

```python
from app.modules.delay_analysis.application.run_creation.run_creation_service import (
    create_analysis_run,
)

await lock_company(db, company_id)  # 호출하는 쪽이 L1 을 먼저 잡는다
result = await create_analysis_run(
    db,
    company_id=company_id,
    delay_event_id=delay_event_id,
    trigger_reason="SCHEDULE_WORSENED",  # 또는 EPISODE_STARTED·SCHEDULE_IMPROVED·STAGE_CHANGED·DELAY_RECOVERED·USER_REANALYSIS
    trigger_shipment_schedule_snapshot_id=snapshot_id,  # AUTO 일 때만
    # requested_by_user_id=..., request_reason=...      # USER_REANALYSIS 일 때만
)
if result.created and result.previous_run is not None:
    ...  # 후속 AUTO 이고 이전 실행이 READY_FOR_REVIEW·NO_RISK 면 같은 L1 안에서 mark_runs_stale (B)
await db.commit()  # 커밋도 호출하는 쪽이 한다
```

| 돌려주는 값 `AnalysisRunCreation` | 뜻 |
| --- | --- |
| `run` | 새로 만든 실행(DETECTED). `created=False`이면 이미 있던 같은 트리거의 AUTO 실행 |
| `created` | `False`이면 같은 지연·같은 트리거 스냅샷의 AUTO 실행이 이미 있어서 **아무것도 바꾸지 않았다** |
| `previous_run` | 새 실행의 부모. `is_latest`를 내린 실행이다. 첫 실행이거나 `created=False`면 `None` |

- **RLS:** 문맥(`app.company_id`, 앱 역할이면 `app.user_id`)은 호출하는 쪽이 연다. 워커·앱 역할 모두 `analysis_runs` INSERT·UPDATE, `analysis_run_data_versions` INSERT 권한이 있다(DDL GRANT).
- **트랜잭션 안의 잠금:** 함수 안에서도 `lock_company`를 한 번 더 잡는다. `pg_advisory_xact_lock`은 같은 트랜잭션에서 다시 잡아도 막히지 않는다. 부모 실행은 L2(`lock_analysis_run`)로 잠근다. 순서는 L1 → L2다.
- **에이전트 시작:** 이 함수는 에이전트 그래프를 띄우지 않는다. 실행을 만든 뒤 백그라운드 실행을 시작하는 일은 커밋 뒤에 따로 한다(다음 작업).

## 2. 기록하는 값

| 열 | 값 | 규칙·근거 |
| --- | --- | --- |
| `analysis_run_id` | UUIDv7 | 명세 8절 |
| `run_code` | `AN-YYMMDD-NNN` | 아래 3절 |
| `version_no` / `parent_analysis_run_id` | 그 지연의 최대 버전 + 1 / 지금 현재 실행(없으면 가장 큰 버전) | ck8, uk2 |
| `is_latest` | 새 실행 true, 부모 false | `analysis_runs_is_latest_uq`. 부모를 먼저 내리고 새 실행을 넣는다 |
| `trigger_type` | `USER_REANALYSIS` → `REANALYSIS`, 나머지 → `AUTO` | ck1·ck17 |
| `trigger_shipment_schedule_snapshot_id` | AUTO만 | ck16. 지연 선적의 스냅샷인지 확인한다 |
| `requested_by_user_id`·`request_reason`·`requested_at` | REANALYSIS만. `requested_at` = `started_at` | ck2·ck3·ck4 |
| `status` | `DETECTED` | 진행 중 상태의 시작 |
| `started_at` | L1을 잡은 뒤의 DB 시각 `clock_timestamp()` | 트랜잭션 시작 시각(`now()`)을 쓰면 먼저 시작했지만 잠금을 늦게 잡은 실행의 시각이 더 이를 수 있다. 그러면 `run_code` 순번과 시각 순서가 어긋난다 |
| `input_seq` | 고객사의 최대 적용 순번 | STALE 판정과 같은 `find_max_applied_seq`. L1 안이라 엔진 `store.input_seq`(시작 시각 이전 수집의 최대)와 같은 값 |
| `operating_rule_set_id` | 기준일에 시작한 버전 중 가장 큰 버전 | 엔진 `snapshot.build`. 없으면 실패(`OPERATING_RULE_MISSING`) |
| `calculation_contract_version` / `allocation_rule_version` | `T14-v1` / `D00-B-b2-v1` | DDL ck34·ck35가 받는 유일한 값 |

### 첫 실행과 그 뒤 실행
- 그 지연에 실행이 없으면 `EPISODE_STARTED`로만 만든다(version 1, 부모 없음).
- 실행이 있으면 `EPISODE_STARTED`가 아닌 사유로만 만든다(ck18). 어기면 `EPISODE_START_MISMATCH`.

### 중복 방지
- 같은 지연·같은 트리거 스냅샷의 AUTO 실행은 1건이다(`analysis_runs_auto_trigger_uq`). 있으면 새로 만들지 않고 그 실행을 `created=False`로 돌려준다.
- 같은 고객사의 동시 호출은 L1에서 차례로 처리된다. 뒤 호출은 앞 트랜잭션이 끝난 뒤 다시 읽으므로, 앞 호출이 커밋했으면 위 규칙으로 `created=False`가 된다.

### STALE은 바꾸지 않는다
- 부모의 `is_latest`만 내린다.
- 후속 AUTO일 때 이전 실행의 STALE 기록(`DELAY_SCHEDULE_CHANGED`·`DELAY_STAGE_CHANGED`)은 B가 같은 L1 안에서 `mark_runs_stale`로 한다.
- 사용자 재분석은 STALE 사유가 아니다.

### 실패
실패하면 값을 바꾸기 전에 `AnalysisRunCreationError(code)`가 나고, DB는 그대로다.

| code | 상황 |
| --- | --- |
| `INVALID_TRIGGER` | 모르는 사유, AUTO인데 스냅샷 없음, AUTO에 요청자·사유가 있음, 재분석인데 요청자 없음·스냅샷 있음 |
| `DELAY_EVENT_NOT_FOUND` | 같은 고객사에 그 지연이 없음. 재분석 API에서는 404로 바꾸면 된다 |
| `SNAPSHOT_NOT_OF_SHIPMENT` | 트리거 스냅샷이 지연 선적의 것이 아님 |
| `EPISODE_START_MISMATCH` | 위 "첫 실행과 그 뒤 실행" 규칙 위반 |
| `OPERATING_RULE_MISSING` | 기준일에 적용 중인 운영 기준이 없음 |
| `RUN_CODE_EXHAUSTED` | 그날 순번 999를 다 씀 |

지연 에피소드가 종료됐는지와 재분석을 허용하는 상태인지(D10)는 이 함수가 보지 않는다. 부르는 쪽(지연 감지 규칙, 재분석 API)이 정한다.

## 3. 표시용 실행 ID `run_code` (0006, D14)

- **형식:** `AN-{기준일 YYMMDD}-{고객사·기준일별 3자리 순번}`. 예: `AN-260102-003`. 형식 CHECK `analysis_runs_run_code_ck`, 고객사 안 고유 `analysis_runs_run_code_uk`, NOT NULL.
- **기준일:** `started_at`의 **UTC 날짜**다. serviceflow "기준일 D = 입력 확정일(started_at의 날짜)", 수집 하루 경계(UTC), 엔진의 운영 기준 선택과 맞춘다.
- **새 실행:** L1 안에서 그날 가장 큰 순번 + 1을 붙인다.
- **기존 행(0006 upgrade):** 고객사·기준일별로 (`started_at`, `analysis_run_id`) 순서대로 `001`부터 채운다. overlay 21건은 `AN-260101-001` ~ `AN-260202-001`이 된다. downgrade 뒤 다시 upgrade해도 같은 값이 나오는 것을 확인했다.
- **팀 확인이 필요한 것(제안 상태):**
  - 기준일을 KST로 할지
  - 같은 시각 실행의 순서(실행 ID 순)

  명세 D14는 "고객사·일자별 순번"까지만 정했고, overlay 실행 21건에 넣을 값은 미정으로 남아 있다.

## 4. 데이터 버전 `record_input_data_versions`

- **규칙:** 수집 도메인(PRODUCT·WAREHOUSE·ORDER·SHIPMENT·INVENTORY)마다, **적용 순번(`applied_seq`)이 실행의 입력 순번(`input_seq`) 이하인 가장 최근 수집 1건**을 기록한다. 적용 순번이 없는(실패·진행 중) 수집은 고르지 않는다.
- **왜 순번인가:** serviceflow 5.2 "왜 시각이 아니라 순번인가"와 STALE 판정이 순번을 기준으로 쓴다. 시작 시각으로 자르면 늦게 시작한 수집이 먼저 커밋되는 경우 등에서 입력 확정 뒤 변경이 섞이거나 빠질 수 있다(PR #19 리뷰에서 같은 기준을 조회 도구 계약에도 적용).
- **엔진과의 관계:** 엔진 `snapshot.build`는 같은 뜻을 시작 시각(`confirm_t`)으로 고른다. overlay의 검증 통과 실행 20건(100행)은 두 규칙 모두 저장값과 같다(DB 조회와 시험으로 확인).
- **부르는 시점:** overlay에서 입력 검증에 실패한 ON_HOLD 실행 1건은 데이터 버전이 0건이다. 그래서 `create_analysis_run`이 기록하지 않는다. 입력 검증(GLOW-100)을 통과한 뒤 부른다.
- **여러 번 불러도 같다:** 이미 기록된 수집 ID는 다시 넣지 않는다(`analysis_run_data_versions_uk2`).
- **serviceflow와 맞춤(11-05 해소):** 처음에는 serviceflow 5.2 308줄이 "실제로 읽은 행의 수집 ID만 기록, 도메인별 최신 수집이라는 이유만으로 기록하지 않는다"고 적어 서버(정답 데이터·엔진과 같은 도메인별 최신, 단 순번 기준)와 달랐다. 설계 문서 `1ea280b`(glowship-plan)가 serviceflow 5.2를 "수집 도메인마다 적용 순번이 입력 순번 이하인 가장 최근 수집 1건"(309·331·379·398줄, 개정 표)으로 고쳐 지금은 서버 구현과 같다.

## 5. 검증

| 구분 | 내용 |
| --- | --- |
| overlay 대조 | 21건 모두 운영 기준이 같다. 검증을 통과한 20건은 데이터 버전(도메인 5개)이 같고, ON_HOLD 1건은 0건이다. 입력 순번 규칙도 21건이 모두 같았다(DB 조회) |
| 실제 DB 시험 | `tests/modules/delay_analysis/test_run_creation_service.py` 15건: 후속 AUTO(버전·부모·is_latest 이동·STALE 그대로·입력 순번·운영 기준·run_code), 같은 트리거 두 번 → 1건, 재분석 요청자·요청 시각, 같은 날 연속 순번, 새 에피소드 첫 실행, 잘못된 요청 5가지 → 바뀐 것 없음, 다른 선적 스냅샷, 없는 지연, 데이터 버전 두 번 기록, overlay 대조, 동시 호출이 L1에서 기다림 |
| 단위 시험 | `tests/modules/delay_analysis/test_run_code.py` 5건 |
| 음성 시험 | 고객사 잠금을 빼면 동시 호출 시험이 실패한다(뒤 호출이 고유 인덱스에서 기다림). `is_latest` 내리기를 빼면 6건, AUTO 중복 확인을 빼면 1건, 데이터 버전 정렬을 뒤집으면 overlay 대조 1건이 실패한다. 데이터 버전의 순번 조건(`applied_seq <= input_seq`)을 빼도 실패한다 |
| 0006 | 임시 DB(원본 템플릿 복사)에서 확인했다. DDL+overlay 복사본은 0004→0005(건너뜀)→0006, 21건이 모두 채워지고 고유하다. 되돌린 뒤 다시 올려도 같은 값이 나온다. 빈 DB 0001→0006은 `alembic check` 변경 없음. 열만 있고 제약이 없는 DB는 멈추고, 열·제약이 다 있는 DB는 그대로 둔다 |

## 6. 작업 중 실패와 원인
- **시험 SQL의 `':EP'`:** `text()`가 콜론 뒤를 바인드 변수로 읽어 실패했다. `\:`로 이스케이프했다. 0005 때와 같은 실수다.
- **0006 확인 SQL:** `conname||contype`에서 `"char"` 연산자가 모호하다는 오류가 났다. `::text`로 고쳤다. 이것도 예전과 같은 실수다.
- **열 주석:** 0006에 열 주석을 넣었더니 빈 DB 경로의 `alembic check`이 모델과 다르다고 잡았다. 다른 분석 열처럼 주석을 빼서 맞췄다.
- **동시 호출 시험의 정리:** 음성 시험에서 시험이 실패할 때 뒤 연결의 작업이 끝나기 전에 연결을 닫아 연결 오류가 함께 났다. 앞 트랜잭션을 먼저 되돌리고 뒤 작업이 끝난 뒤 닫도록 고쳤다.
- **데이터 버전 기준:** 처음에는 엔진처럼 시작 시각(`started_at`)으로 골랐다. PR #19 리뷰(조회 도구 기준은 순번)를 보고 serviceflow 5.2와 맞지 않는 것을 알았다. overlay에서 두 규칙의 결과가 같은 것을 확인하고 순번 기준으로 바꿨다.
