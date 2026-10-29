# GlowShip Server

GlowShip 백엔드 서버 저장소입니다.
**API 서버와 AI 에이전트를 모두 FastAPI 하나로 구현합니다.**

> 프로젝트 한 줄 소개를 여기에 작성합니다.

## 관련 저장소

| 저장소 | 설명 |
| --- | --- |
| [glowship-web](https://github.com/K-GlowShip/glowship-web) | 웹 클라이언트 (React + Vite) |
| [glowship-server](https://github.com/K-GlowShip/glowship-server) | 백엔드 서버 (FastAPI: API + AI 에이전트) — 현재 저장소 |

## 기술 스택

- **Python 3.14**, **uv**: 패키지·가상환경 관리
- **FastAPI**: REST API 서버 및 AI 에이전트 엔드포인트
- **PostgreSQL 17**, **SQLAlchemy 2 (async)**, **Alembic**: DB·ORM·마이그레이션
- **LangGraph**: 지연 분석 에이전트 그래프
- **pytest**: 테스트
- **Ruff**: 린트 및 포맷 (GitHub Actions에서 검사)

## 애플리케이션 아키텍처

일반 비즈니스 API와 AI 에이전트 기능을 하나의 FastAPI 애플리케이션 안에서 관리하며,
도메인별 모듈(`app/modules/{도메인}/`)과 공통 기반(`app/core/`)으로 나눕니다.

### 폴더 구조

```
app/
├── main.py                        # FastAPI 앱 진입점 (미들웨어·예외 핸들러·라우터 등록)
├── core/                          # 모든 모듈이 공유하는 기반
│   ├── config.py                  # 환경 변수 Settings (.env 로딩)
│   ├── database.py                # 비동기 DB 엔진, 세션 팩토리, 요청 단위 세션(get_db)
│   ├── orm.py                     # DeclarativeBase(PK 이름 규칙·타입 매핑), uuid_pk, created_at/updated_at 공통 컬럼
│   ├── model_registry.py          # 모든 모듈의 ORM 모델 등록 (Alembic·적재 스크립트가 사용)
│   ├── exceptions.py              # 공통 에러 응답 형식, BaseErrorCode, BusinessException, 핸들러
│   ├── middleware.py              # CORS, 미처리 예외 → JSON 500 변환
│   └── security/
│       └── hashing.py             # bcrypt 비밀번호 해싱
└── modules/                       # 도메인 모듈
    ├── user/
    │   ├── dependencies.py        # 의존성 조립 (DB 세션 → Repository → Service)
    │   ├── presentation/          # API 라우터, 요청·응답 스키마
    │   ├── application/           # 서비스(비즈니스 로직), 비밀번호 정책, 도메인 예외
    │   └── infrastructure/        # ORM 모델, enum, Repository
    ├── company/                   # companies, admin_assignments
    ├── master_data/               # skus, warehouses, external_entity_mappings, market_leadtime_references
    ├── operating_rule/            # operating_rule_sets, operating_lead_times
    ├── data_sync/                 # data_sync_runs, data_validation_issues, data_change_records
    ├── order/                     # orders, order_lines (+ aux: 출고일·대기 이력)
    ├── shipment/                  # shipments, shipment_lines, shipment_schedule_snapshots
    ├── inventory/                 # inventory_daily (+ aux: 정기 발주 검토)
    ├── delay_analysis/            # delay_events, analysis_runs 외 분석 결과 5개 + 분석 에이전트(application/agent)
    └── decision/                  # member_drafts, final_decisions
migrations/                        # Alembic 마이그레이션 (versions/0001: v5 스키마 + RLS)
scripts/
└── seed_synthetic_data.py         # 합성데이터 적재 (manifest 검증 → COPY → 행 수 검증)
```

> 사용자 모듈을 제외한 모듈은 현재 `infrastructure/`의 ORM 모델만 있습니다. API를 만들 때 같은 세 계층을 추가합니다.

### 모듈 계층

각 도메인 모듈은 세 계층으로 나누며, 의존 방향은 위에서 아래로만 흐릅니다.

| 계층 | 역할 | 예시 (`user`) |
| --- | --- | --- |
| `presentation` | HTTP 요청·응답 처리. 라우터와 Pydantic 스키마만 둡니다. | `user_api_router.py`, `user_request_schemas.py` |
| `application` | 비즈니스 로직. 도메인 규칙을 검증하고 `BusinessException`을 던집니다. | `user_service.py`, `password.py`, `exception/user_exceptions.py` |
| `infrastructure` | DB 접근. ORM 모델과 Repository를 둡니다. | `user.py`, `user_enums.py`, `repository/user_repository.py` |

- `dependencies.py`에서 `get_db` → Repository → Service 순으로 조립해 라우터에 주입합니다.
- 새 도메인을 추가할 때는 `app/modules/{도메인}/` 아래에 같은 세 계층을 만들고, `main.py`에 라우터를 등록합니다.

### 요청 처리 흐름

```
클라이언트
  → CORSMiddleware (가장 바깥)
  → UnhandledExceptionMiddleware (미처리 예외 → JSON 500)
  → Router (presentation)
  → Service (application)
  → Repository (infrastructure)
  → PostgreSQL
```

- 요청 단위 세션(`get_db`)은 정상 종료 시 commit, 예외 시 rollback 후 재전파합니다.
- Repository는 `flush`까지만 수행하고, commit은 세션이 종료될 때 한 번에 처리합니다.

### 에러 응답

모든 에러 응답은 아래 형식으로 통일합니다.

```json
{ "code": "EMAIL_DUPLICATED", "message": "요청 이메일은 이미 사용중입니다.", "request_id": "..." }
```

| 상황 | 상태 코드 | 처리 위치 |
| --- | --- | --- |
| 도메인 규칙 위반 (`BusinessException`) | 모듈별 `ErrorCode`에 정의 (400, 409 등) | `core/exceptions.py` 핸들러 |
| 요청 형식 오류 (`RequestValidationError`) | 422 `VALIDATION_ERROR` | `core/exceptions.py` 핸들러 |
| 없는 경로 / 허용되지 않은 메서드 (`HTTPException`) | 404 `NOT_FOUND` / 405 `METHOD_NOT_ALLOWED` | `core/exceptions.py` 핸들러 |
| 처리되지 않은 예외 | 500 `INTERNAL_SERVER_ERROR` | `core/middleware.py` |

- 모듈별 에러 코드는 `BaseErrorCode`를 상속해 `(status_code, code, message)` 튜플로 선언합니다.
- 서비스 계층에서는 `BusinessException`을 상속한 예외만 던지고, HTTP 응답 변환은 핸들러가 담당합니다.

## 프로젝트 실행 방법

### 요구 사항

| 도구 | 버전 / 비고 |
| --- | --- |
| Python | 3.14 (`.python-version`) |
| [uv](https://docs.astral.sh/uv/) | 패키지·가상환경 관리 |
| Docker Desktop | PostgreSQL 컨테이너 실행 |

### 1. 의존성 설치

```bash
uv sync
```

`.venv`가 생성되고 `pyproject.toml`의 의존성(dev 그룹 포함)이 설치됩니다.


| 변수 | 설명 |
| --- | --- |
| `APP_ENV` | `local` / `dev` / `prod` |
| `POSTGRES_*` | docker-compose와 앱이 함께 사용하는 DB 접속 정보 |
| `CORS_ORIGINS` | 허용할 프론트엔드 오리진 목록 (JSON 배열 문자열) |

### 2. 서버 실행

```bash
make dev
```

`make dev`는 Docker Desktop 기동 → PostgreSQL 컨테이너 기동(healthcheck 대기) → 마이그레이션 적용(`alembic upgrade head`) → `uvicorn` 실행을 순서대로 수행합니다.

| 명령 | 설명 |
| --- | --- |
| `make dev` | DB 기동·마이그레이션 후 개발 서버 실행 (`--reload`) |
| `make docker-compose` | PostgreSQL 컨테이너만 기동 |
| `make docker-compose-down` | 컨테이너 종료 |
| `make docker-compose-reset` | 컨테이너와 데이터 볼륨을 삭제하고 재기동 |
| `make db-migrate` | 마이그레이션 적용 (`alembic upgrade head`) |
| `make db-seed SEED_DATA_DIR=<폴더>` | 합성데이터 적재 (테이블이 비어 있을 때만) |
| `make db-reseed SEED_DATA_DIR=<폴더>` | 테이블을 비우고 합성데이터 다시 적재 |

Makefile 없이 직접 실행하려면 다음과 같이 합니다.

```bash
docker compose up -d --wait
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

### 3. DB 스키마와 합성데이터

- 스키마 기준: `erd_glowship_postgres_v5_test_ddl.sql` (27개 테이블). 단, `users`는 `created_at`·`updated_at` 컬럼과 `varchar(50·80·30)` 길이를 유지합니다.
- 테이블은 서버 기동 시 만들지 않고 **Alembic 마이그레이션으로만** 생성·변경합니다 (`migrations/versions/`).
- ORM 모델은 각 모듈의 `infrastructure/`에 있고, `app/core/model_registry.py`에 등록해야 autogenerate 대상이 됩니다.
- 고객사 RLS(역할 `glowship_v4_owner`·`glowship_v4_app`·`glowship_v4_worker`, 정책, Admin 함수)는 초기 마이그레이션에 포함됩니다. 로컬은 superuser로 접속하므로 RLS가 적용되지 않습니다.

모델을 바꾼 뒤 마이그레이션 만들기:

```bash
uv run alembic revision --autogenerate -m "변경 내용"
uv run alembic upgrade head
```

합성데이터(2025, v5) 적재:

```bash
make db-seed SEED_DATA_DIR="<합성데이터 폴더>/synthetic_2025_v5/20261028T060456Z"
```

- 적재 전 `manifest.json`의 sha256·행 수로 원본 파일을 검증하고, 하나라도 다르면 중단합니다.
- FK 의존 순서대로 `COPY` 하며, 전체가 하나의 트랜잭션이라 실패 시 아무것도 남지 않습니다.
- `_p4_auxiliary/`의 부속 CSV 3개는 `aux` 스키마(`derived_order_dispatch_dates`, `wait_history`, `regular_order_reviews`)에 적재됩니다. 업무 코드(`company_code`, `order_code` 등)로 연결하며 FK는 없습니다.
- 원본 CSV·manifest는 수정하지 않습니다.

### 4. API 문서 확인

서버가 뜨면 아래 주소에서 Swagger UI로 API를 확인하고 호출할 수 있습니다.

- http://localhost:8000/docs

### 5. 테스트

```bash
uv run pytest
```

테스트는 `tests/` 아래에 모듈 구조와 같은 경로로 둡니다. 현재 테스트는 DB 없이 실행됩니다.

## 지연 분석 에이전트

`agentflow_glowship.md` 기준의 LangGraph 그래프입니다. 코드는 `app/modules/delay_analysis/application/agent/`에 있습니다.

```
분석 준비 ─┬─ NO_DELAY·ON_HOLD ─────────────────────────────▶ 종료
           └▶ 조정 에이전트 ⇄ 재고 영향 분석 에이전트   (위험 주문 없음 → NO_RISK 종료)
                   ⇄ 대응안 분석 에이전트
                   └▶ 대응안 비교 ▶ 추천 근거 설명 ▶ 결과 검증 ▶ 종료(READY_FOR_REVIEW)
```

| 파일 | 내용 |
| --- | --- |
| `state.py` | 그래프 상태, 실행 단계(`AnalysisStep`), 종료 상태(`AnalysisOutcome`) |
| `nodes/` | 노드 7개. 현재는 빈 결과만 반환하는 뼈대이며 `TODO`에 붙일 도구가 적혀 있습니다 |
| `graph.py` | 노드 연결, 조정 에이전트의 다음 분석 선택, 종료 분기 |
| `run_status.py` | 노드 경계 STALE 확인. 조정 에이전트·설명 노드 진입 전에 실행 상태를 조회합니다 |

- 에이전트는 업무 DB에 쓰지 않고 결과만 반환합니다. 저장·STALE 판정은 서비스 책임입니다.
- 실행 상태 조회기는 `config["configurable"]["run_status_reader"]`로 주입합니다. 주입하지 않으면 STALE 확인을 건너뜁니다.
- 개발 확인용 `POST /api/v1/dev/agent/dry-run`으로 그래프를 한 번 실행해 거친 노드 순서를 볼 수 있습니다 (`APP_ENV=prod`에서는 등록되지 않음).

## 코드 스타일

`main`, `develop` 브랜치로의 push 및 Pull Request 시 GitHub Actions에서 Ruff로 린트와 포맷을 검사합니다.
`pyproject.toml`이 추가되기 전까지는 검사를 건너뜁니다.

## 브랜치 전략

> 팀 합의 후 작성합니다. (예: `main` / `develop` / `feature/*`)

## 커밋 컨벤션

> 팀 합의 후 작성합니다. (예: `feat:`, `fix:`, `docs:`, `chore:`)
