# 데이터 접근 원칙 (ORM · SQLAlchemy 식 · 직접 SQL)

서버가 DB를 읽고 쓰는 방식을 정합니다. 지금 develop 코드가 따르고 있는 관례를 문서로 옮긴 것이고, 아직 정하지 않은 것은 9절에 따로 적었습니다.

- **상태:** 초안(2026-11-04). 백엔드 개발자 세 명의 리뷰로 합의한 뒤 확정합니다. `app/core`는 개발자 1 담당이라 개발자 1의 리뷰가 필요합니다.
- **범위:** `app/` 아래 Python 코드. Alembic 마이그레이션(`migrations/`)과 데이터 적재 스크립트(`scripts/`)는 각 파일 머리말의 규칙을 따릅니다.
- **함께 보는 문서:** glowship-plan `erd_glowship_v6_2_overview.md` 4절(RLS·역할 권한), 서버 `README.md` "고객사 범위(RLS 컨텍스트)"

## 1. 한눈에 보기

| 상황 | 방식 | 예 |
| --- | --- | --- |
| 한 행을 읽고 바꾸는 처리 | ORM 엔터티(`select(Model)`, 속성 변경, `db.add` + `flush`) | `user_repository.py`, `company_repository.py`, `stale_service.py`의 실행 상태 변경 |
| 여러 행을 한 번에 넣거나 바꾸는 처리 | ORM 모델을 쓴 SQLAlchemy 식(`insert(Model)`, `update(Model)`, `select(Model.col)`) | `ingest_repository.py`(수집 적재), `validation_issue_repository.py` |
| 화면용 목록·집계 조회 | 직접 SQL(`text()` + `bindparam`) | `delay_event_read_repository.py`, `sku_repository.py`, `order_repository.py`, `inventory_repository.py`, `data_sync_run_repository.py` |
| PostgreSQL 기능 호출 | 직접 SQL | RLS 컨텍스트 `set_config`(`core/rls_context.py`), 잠금 `pg_advisory_xact_lock`(`core/locks.py`), `SECURITY DEFINER` 함수 `app_admin_create_client_user()`(`user_repository.py`) |
| 표 사이 연관 | `relationship()`을 쓰지 않음. 필요한 쿼리에서 명시적으로 JOIN | develop 전체 0건 |

어느 방식이든 DB 접근은 각 모듈의 `infrastructure/repository/`에서만 합니다. 서비스와 라우터는 SQL이나 ORM 쿼리를 직접 만들지 않습니다(잠금·RLS 헬퍼 호출은 예외, 5절).

## 2. ORM 엔터티

**언제:** 한 행(또는 몇 행)을 키로 읽어 그 객체의 값을 바꾸거나, 한 행을 새로 만들 때.

- 조회는 `select(Model).where(...)`로 하고 `scalar_one_or_none()` 등으로 받습니다.
- 변경은 객체 속성을 바꾼 뒤 `flush()`합니다. 커밋은 하지 않습니다(6절).
- 세션은 `autoflush=False`, `expire_on_commit=False`입니다(`core/database.py`). 그래서 같은 트랜잭션에서 바꾼 값을 다른 쿼리로 다시 읽으려면 그 전에 `flush()`가 필요합니다.
- 다른 트랜잭션이 바꾼 값을 확실히 읽어야 하면(예: 잠근 뒤 최신 값) ORM 객체 대신 열을 다시 조회하거나 잠금 헬퍼가 돌려주는 값을 씁니다(`lock_analysis_run`).

## 3. ORM 모델을 쓴 SQLAlchemy 식

**언제:** 수백~수만 행을 한 문장으로 넣거나 바꿀 때. 객체를 하나씩 만들면 느리고 메모리를 많이 씁니다.

- `insert(Model)`에 딕셔너리 목록을 넘겨 한 번에 넣습니다. `update(Model).where(...)`로 조건에 맞는 행을 한 번에 바꿉니다.
- 표와 열 이름은 ORM 모델에서 가져오므로, 이름이 틀리면 실행 전에 드러납니다.
- 키 목록으로 조회할 때는 바인드 변수 개수 한도를 넘지 않게 나눠서 조회합니다(`ingest_repository._chunked`).

## 4. 직접 SQL (`text()`)

**언제:** 아래 중 하나라도 해당하면 직접 SQL을 씁니다.

- CTE, `EXISTS`, `DISTINCT ON`, `LATERAL`, 창 함수처럼 SQLAlchemy 식으로 쓰면 읽기 어려워지는 조회
- 목록과 건수를 같은 조건으로 세는 조회(같은 CTE를 두 문장이 공유)
- "값이 없으면 통과하는" 선택 필터가 여럿인 조회(`CAST(:x AS uuid) IS NULL OR col = :x`)
- PostgreSQL 함수·잠금·설정 호출

**규칙**

- **바인드 변수만 씁니다.** 사용자 입력을 문자열로 이어 붙이지 않습니다.
- **타입이 필요한 변수는 `bindparam(name, type_=...)`로 정합니다.** 선택 필터의 `NULL` 비교(`CAST(:x AS uuid) IS NULL`)에 쓰는 변수와 `uuid`·`timestamptz`·`date` 값을 넘기는 변수가 그렇습니다. DB가 `NULL`과 값 변환을 바르게 하도록 하기 위해서입니다. 목록·집계 조회 문장은 모두 여기에 해당합니다.
  - PostgreSQL 함수의 text 인자처럼 문자열로 받는 값은 타입을 정하지 않고 문자열로 넘깁니다. 다른 타입이 필요하면 SQL 안에서 `CAST`합니다. 예: `core/locks.py`(`CAST(:company_id AS text)`), `core/rls_context.py`(`set_config`), `user_repository.py`의 `app_admin_create_client_user()` 이메일·이름 인자.
- **LIKE 검색어는 이스케이프합니다.** `\`·`%`·`_`를 글자 그대로 찾도록 바꾸고 `ESCAPE '\'`를 씁니다. 지금 같은 함수 `_like_pattern`이 세 리포지토리(`sku`·`order`·`inventory`)에 따로 있습니다(9절).
- **SQL 문장은 모듈 상수로 둡니다.** `_LIST_PAGE`, `_COUNT_PAGE`처럼 이름을 붙이고, 문장 위 주석에 명세의 어느 정의를 옮긴 것인지 적습니다. 리뷰에서 명세와 한 줄씩 대조하기 위해서입니다.
- **결과는 dataclass로 바꿉니다.** 리포지토리는 `Row`를 그대로 내보내지 않고 `XxxRow(**row._mapping)`처럼 이름 있는 값으로 돌려줍니다.
- **식별자(표·열 이름)를 변수로 받지 않습니다.** 정렬 열을 요청으로 바꿔야 하면 허용 목록에서 고른 상수 문장을 씁니다.

## 5. 고객사 범위 (RLS와 `company_id` 조건)

- 고객사 데이터를 다루는 트랜잭션은 먼저 `set_rls_context()`로 `app.user_id`·`app.company_id`를 설정합니다. API는 `get_tenant_context` 의존성이 해 줍니다. 워커(수집·분석)는 `user_id` 없이 `company_id`만 넣습니다.
- **쿼리에도 `company_id` 조건을 직접 겁니다.** 로컬 개발 DB는 superuser로 접속해 RLS가 우회됩니다(glowship-plan ERD 설명 4.6). 조건을 넣어 두면 RLS가 있든 없든 같은 결과가 나옵니다. JOIN도 `(company_id, id)` 복합 키로 겁니다.
- 앱 DB 역할에 권한이 없는 쓰기(예: `users` INSERT)는 DB의 `SECURITY DEFINER` 함수로만 합니다.

## 6. 트랜잭션과 커밋

- **API 요청:** 요청 단위 세션(`get_db`)이 정상 종료면 커밋, 예외면 롤백합니다. 리포지토리와 서비스는 커밋하지 않고 필요하면 `flush()`만 합니다.
- **잠금:** 고객사 잠금(L1, `lock_company`)과 실행 잠금(L2, `lock_analysis_run`)은 트랜잭션 잠금이라 커밋·롤백 때 풀립니다. 잠금 순서는 L1 → L2입니다(serviceflow 5.2절).
- **예외(지금 코드):** 수집 적재 서비스(`ingest_service.py`)는 "수집 시작" 기록을 먼저 커밋하고, 본 처리를 다른 트랜잭션에서 커밋 또는 롤백한 뒤 실패를 기록합니다. 실패해도 수집 실행 행이 남아야 하기 때문입니다. 이처럼 서비스가 직접 커밋하는 경우의 기준은 9절에서 정합니다.

## 7. `relationship()`을 쓰지 않는 이유

- 대부분의 표가 `(company_id, id)` 복합 키로 서로 참조합니다. 외래 키는 `ForeignKeyConstraint`로 DB 제약만 선언합니다.
- 고객사 경계를 RLS와 명시적 `company_id` 조건으로 지킵니다. 관계 속성으로 따라가는 조회는 그 조건이 쿼리에 드러나지 않습니다.
- 비동기 세션에서 관계 속성의 지연 로딩은 예외를 냅니다. 필요한 연관 데이터는 그 쿼리에서 JOIN해 한 번에 읽습니다.

## 8. 테스트

| 방식 | 시험 | 예 |
| --- | --- | --- |
| 공통 | 가짜 세션으로 리포지토리가 만든 문장·파라미터·행 변환을 확인(DB 없음) | `test_order_repository.py`, `test_data_sync_run_repository.py` |
| 직접 SQL | 실제 DB 읽기 전용 스모크 시험(`db_session`, 끝나면 롤백). DB가 없거나 마이그레이션 head가 아니면 건너뜀 | `test_order_repository_sql_smoke.py`, `test_repository_sql_smoke.py` |
| 쓰기 | 실제 DB 쓰기 시험(`db_write_session`, SAVEPOINT 모드, 끝나면 롤백) | `test_ingest_service_sql.py`, `test_ingest_repository_sql.py` |
| ORM 모델 | CHECK 허용 값과 코드 열거형이 같은지(DB 없음) | `test_analysis_model_checks.py`, `test_record_validation.py` |

직접 SQL을 새로 쓰면 스모크 시험을 함께 넣습니다. SQL 오타나 열 이름 오류는 가짜 세션 시험으로는 잡히지 않습니다.

## 9. 아직 정하지 않은 것

| # | 내용 | 지금 상태 |
| --- | --- | --- |
| 1 | 서비스가 직접 커밋해도 되는 경우 | 수집 적재만 직접 커밋(6절). 백그라운드 분석 실행(GLOW-99 이후)도 같은 구조가 될 가능성이 큼 |
| 2 | `_like_pattern` 공통화 | 세 리포지토리에 같은 함수. `app/core`로 옮길지(개발자 1) |
| 3 | 쿼리 형식 타입의 위치 | `IsoAwareDatetime`은 `app/core/query_types.py`, `IsoDate`는 `app/core/schemas.py`. 한 곳으로 모을지(개발자 1) |
| 4 | 예외를 허용하는 조건 | 위 기준과 다르게 써야 할 때는 PR 본문 "리뷰 포인트"에 이유를 적고 리뷰에서 합의 |
