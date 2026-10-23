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

## 설계 방침

- 일반 비즈니스 API와 AI 에이전트 기능을 하나의 FastAPI 애플리케이션 안에서 관리합니다.
- 계층을 분리합니다: 라우터(`api`) → 비즈니스 로직(`services`) → 데이터 접근(`repositories`).
- 에이전트 관련 코드는 `agents/` 아래에 모아 일반 API 로직과 구분합니다.

## 예정 폴더 구조

```
glowship-server/
├── .github/
│   └── workflows/
│       └── code-style.yml # Ruff 검사
├── app/
│   ├── main.py            # FastAPI 앱 진입점
│   ├── api/               # 라우터 (엔드포인트 정의)
│   │   └── v1/
│   ├── core/              # 설정, 보안, 공통 의존성
│   ├── services/          # 비즈니스 로직
│   ├── repositories/      # DB 접근 계층
│   ├── models/            # DB 모델
│   ├── schemas/           # Pydantic 요청/응답 스키마
│   └── agents/            # AI 에이전트 (프롬프트, 도구, 워크플로)
├── tests/                 # 테스트
├── .editorconfig
├── .gitignore
├── pyproject.toml         # 의존성 및 Ruff 설정
└── README.md
```

> 위 구조는 초안이며, 개발환경 설정 시 확정합니다.

## 개발환경

개발환경 설정은 추후 진행 예정입니다. 설정이 완료되면 아래 내용을 채웁니다.

- Python 버전
- 가상환경 및 의존성 설치 방법
- 서버 실행 방법 (예: `uvicorn app.main:app --reload`)
- 환경 변수 (`.env.example`)
- API 문서 경로 (`/docs`)

## 코드 스타일

`main`, `develop` 브랜치로의 push 및 Pull Request 시 GitHub Actions에서 Ruff로 린트와 포맷을 검사합니다.
`pyproject.toml`이 추가되기 전까지는 검사를 건너뜁니다.

## 브랜치 전략

> 팀 합의 후 작성합니다. (예: `main` / `develop` / `feature/*`)

## 커밋 컨벤션

> 팀 합의 후 작성합니다. (예: `feat:`, `fix:`, `docs:`, `chore:`)
