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

- **Python**
- **FastAPI**: REST API 서버 및 AI 에이전트 엔드포인트
- **Ruff**: 린트 및 포맷 (GitHub Actions에서 검사)

> DB, ORM, 에이전트 프레임워크, 패키지 관리 도구 등 세부 스택은 개발환경 설정 시 결정합니다.

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
│   ├── orm.py                     # DeclarativeBase, created_at/updated_at 공통 컬럼
│   ├── exceptions.py              # 공통 에러 응답 형식, BaseErrorCode, BusinessException, 핸들러
│   ├── middleware.py              # CORS, 미처리 예외 → JSON 500 변환
│   └── security/
│       └── hashing.py             # bcrypt 비밀번호 해싱
└── modules/                       # 도메인 모듈 (예: user)
    └── user/
        ├── dependencies.py        # 의존성 조립 (DB 세션 → Repository → Service)
        ├── presentation/          # API 라우터, 요청·응답 스키마
        ├── application/           # 서비스(비즈니스 로직), 비밀번호 정책, 도메인 예외
        └── infrastructure/        # ORM 모델, enum, Repository
```

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

`make dev`는 Docker Desktop 기동 → PostgreSQL 컨테이너 기동(healthcheck 대기) → `uvicorn` 실행을 순서대로 수행합니다.

| 명령 | 설명 |
| --- | --- |
| `make dev` | DB 기동 후 개발 서버 실행 (`--reload`) |
| `make docker-compose` | PostgreSQL 컨테이너만 기동 |
| `make docker-compose-down` | 컨테이너 종료 |
| `make docker-compose-reset` | 컨테이너와 데이터 볼륨을 삭제하고 재기동 |

Makefile 없이 직접 실행하려면 다음과 같이 합니다.

```bash
docker compose up -d --wait
uv run uvicorn app.main:app --reload
```

### 4. API 문서 확인

서버가 뜨면 아래 주소에서 Swagger UI로 API를 확인하고 호출할 수 있습니다.

- http://localhost:8000/docs

> **주의**: 현재는 서버 기동 시 `lifespan`에서 모든 테이블을 drop 후 create합니다.
> 재시작할 때마다 데이터가 초기화되므로 로컬 개발 용도로만 사용하고, 마이그레이션 도구 도입 전까지는 운영 환경에 배포하지 않습니다.

## 코드 스타일

`main`, `develop` 브랜치로의 push 및 Pull Request 시 GitHub Actions에서 Ruff로 린트와 포맷을 검사합니다.
`pyproject.toml`이 추가되기 전까지는 검사를 건너뜁니다.

## 브랜치 전략

> 팀 합의 후 작성합니다. (예: `main` / `develop` / `feature/*`)

## 커밋 컨벤션

> 팀 합의 후 작성합니다. (예: `feat:`, `fix:`, `docs:`, `chore:`)
