# FastAPI 서버 이미지 (CD 파이프라인: GitHub Actions → ECR → EC2)
#   - 2단계 빌드: builder 에서 uv 로 의존성을 .venv 에 설치하고, runtime 은 그 .venv 와 코드만 복사한다 (uv·빌드 캐시 제외).
#   - pyproject 의 requires-python 이 3.14 이상이라 python:3.14-slim 을 쓴다 (.python-version 과 같은 판).
#   - 설정은 .env 를 굽지 않고 환경변수로 받는다 (app/core/config.py, .dockerignore 가 .env 를 제외).
#     필수: APP_ENV(local·dev·prod), POSTGRES_HOST·PORT·USER·PASSWORD·DB, JWT_SECRET_KEY(32자 이상). 운영은 APP_ENV=prod, VIRTUAL_CLOCK_START= (비움).
#   - 엔진 코드(GLOWSHIP_ENGINE_DIR)는 저장소에 없어 이미지에 넣지 않는다. 운영에서 분석 기능이 필요하면
#     배포 시 볼륨으로 붙이고 GLOWSHIP_ENGINE_DIR 을 그 경로로 준다 (docs/analysis/engine-local-path.md).
#   - 마이그레이션은 이미지에 포함한다. 배포 스크립트가 같은 이미지로 `alembic upgrade head` 를 먼저 실행한다.
#
# 로컬 확인:
#   docker build -t glowship-server:local .
#   docker run --rm -p 8000:8000 --env-file .env -e POSTGRES_HOST=host.docker.internal glowship-server:local

ARG PYTHON_VERSION=3.14

# ---------- builder: 의존성 설치 ----------
FROM python:${PYTHON_VERSION}-slim AS builder

# uv 바이너리만 공식 이미지에서 복사한다 (로컬 uv 와 같은 판으로 고정해 uv.lock 해석이 같게).
COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# 의존성 목록만 먼저 복사해 코드가 바뀌어도 이 레이어는 캐시를 쓴다.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# ---------- runtime: 코드 + .venv ----------
FROM python:${PYTHON_VERSION}-slim AS runtime

# 배포 스크립트의 옛 이미지 정리(docker image prune --filter label=…)가 이 프로젝트 이미지만 고르는 표식
LABEL app=glowship-server

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

# root 로 실행하지 않는다.
RUN groupadd --system app && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app

# 코드·의존성은 root 소유로 둔다(읽기·실행만 가능). 실행 사용자 app 이 코드를 고칠 수 없게 하기 위해서다(PR #92 리뷰).
COPY --from=builder /app/.venv /app/.venv
# 실행에 필요한 것만 복사한다 (tests·docs·resource 는 .dockerignore 로 제외).
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini pyproject.toml ./

USER app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
