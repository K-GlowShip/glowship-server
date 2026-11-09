#!/usr/bin/env bash
# dev 서버(EC2) 배포 스크립트. GitHub Actions(deploy-dev.yml)가 SSM RunShellScript 로 이 파일 내용을 EC2 에서 실행한다.
# EC2 에 미리 복사해 둘 필요가 없다. 저장소의 이 파일이 유일한 원본이다.
#
# 하는 일 (순서대로)
#   1. ECR 로그인 (EC2 인스턴스 역할 권한. 키 없음)
#   2. 새 이미지 pull
#   3. 같은 이미지로 alembic upgrade head (서버 교체 전에 DB 를 먼저 맞춘다)
#   4. 기존 컨테이너를 내리고 새 이미지로 기동
#   5. 헬스 확인. 실패하면 직전 이미지로 되돌리고 종료 코드 1
#   6. 쓰지 않는 옛 이미지 정리
#
# 입력 (환경변수. 워크플로가 IMAGE 와 AWS_REGION 을 넣고 나머지는 기본값)
#   IMAGE          필수. 배포할 이미지 (예: 123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/glowship-server:<sha>)
#   AWS_REGION     ECR_LOGIN=1 일 때 필수
#   APP_NAME       컨테이너 이름. 기본 glowship-server
#   ENV_FILE       서버 설정 파일. 기본 /etc/glowship/server.env (아래 "EC2 준비" 참고)
#   PORT           호스트에 열 포트. 기본 8000 (컨테이너 안은 항상 8000)
#   ENGINE_DIR     선택. 분석 엔진 폴더(glowship_engine·synth2025_v5 의 부모). 주면 /opt/engine 으로 읽기 전용 마운트하고
#                  GLOWSHIP_ENGINE_DIR=/opt/engine 을 컨테이너에 준다. 비우면 엔진 없이 뜬다(분석 기능 비활성).
#   HEALTH_PATH    헬스 확인 경로. 기본 /health (app/core/health.py. 서버가 뜨고 DB 에 SELECT 1 이 되면 200, 아니면 503)
#   HEALTH_TIMEOUT 헬스 확인 대기 초. 기본 60
#   ECR_LOGIN      1(기본)이면 ECR 로그인. 로컬 이미지로 시험할 때 0
#
# EC2 준비 (한 번만)
#   - SSM Agent 동작 (Amazon Linux 2023 기본 포함), 인스턴스 역할에 AmazonSSMManagedInstanceCore + AmazonEC2ContainerRegistryReadOnly
#   - docker, aws cli 설치 및 docker 서비스 기동
#   - ENV_FILE 작성 (root 소유, chmod 600). .env.example 의 키를 dev 값으로 채운다:
#       APP_ENV=dev, POSTGRES_HOST=<RDS 엔드포인트>, POSTGRES_PORT, POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB,
#       JWT_SECRET_KEY(32자 이상), CORS_ORIGINS=["https://<프론트 도메인>"], VIRTUAL_CLOCK_START= (실제 원천이면 비움)
#     GLOWSHIP_ENGINE_DIR 은 이 파일에 쓰지 않는다(ENGINE_DIR 로 마운트 경로를 준다).
#   - 분석 기능을 쓰려면 엔진 폴더를 EC2 에 두고 ENGINE_DIR 로 넘긴다 (docs/analysis/engine-local-path.md)
#
# 주의
#   - 되돌리기는 컨테이너만 직전 이미지로 바꾼다. 이미 적용된 마이그레이션은 되돌리지 않는다(downgrade 는 수동 판단).

set -euo pipefail

: "${IMAGE:?IMAGE 가 필요합니다 (예: <registry>/<repo>:<tag>)}"
APP_NAME="${APP_NAME:-glowship-server}"
ENV_FILE="${ENV_FILE:-/etc/glowship/server.env}"
PORT="${PORT:-8000}"
ENGINE_DIR="${ENGINE_DIR:-}"
HEALTH_PATH="${HEALTH_PATH:-/health}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-60}"
ECR_LOGIN="${ECR_LOGIN:-1}"

log() { printf '[deploy] %s\n' "$*"; }

if [ ! -f "$ENV_FILE" ]; then
  log "설정 파일이 없습니다: $ENV_FILE"
  exit 1
fi

# 컨테이너 실행 인자(마이그레이션·서버 공통). 엔진 폴더가 있으면 읽기 전용으로 붙인다.
run_args=(--env-file "$ENV_FILE")
if [ -n "$ENGINE_DIR" ]; then
  if [ ! -d "$ENGINE_DIR" ]; then
    log "ENGINE_DIR 폴더가 없습니다: $ENGINE_DIR"
    exit 1
  fi
  run_args+=(-v "$ENGINE_DIR:/opt/engine:ro" -e GLOWSHIP_ENGINE_DIR=/opt/engine)
fi

# 1. ECR 로그인
if [ "$ECR_LOGIN" = "1" ]; then
  : "${AWS_REGION:?ECR 로그인에 AWS_REGION 이 필요합니다}"
  registry="${IMAGE%%/*}"
  log "ECR 로그인: $registry"
  aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$registry"
fi

# 2. 이미지 pull (ECR_LOGIN=0 인 로컬 시험에서 이미 있는 로컬 이미지는 pull 하지 않는다)
if [ "$ECR_LOGIN" = "1" ] || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  log "이미지 pull: $IMAGE"
  docker pull "$IMAGE"
else
  log "로컬 이미지 사용: $IMAGE"
fi

# 되돌리기용으로 지금 돌고 있는 이미지를 기억한다 (없으면 빈 값)
previous_image="$(docker inspect --format '{{.Config.Image}}' "$APP_NAME" 2>/dev/null || true)"
log "현재 이미지: ${previous_image:-없음}"

# 3. 마이그레이션 (서버 교체 전)
log "마이그레이션: alembic upgrade head"
docker run --rm "${run_args[@]}" "$IMAGE" alembic upgrade head

start_container() {
  local image="$1"
  docker rm -f "$APP_NAME" >/dev/null 2>&1 || true
  docker run -d \
    --name "$APP_NAME" \
    --restart unless-stopped \
    -p "${PORT}:8000" \
    --log-opt max-size=10m --log-opt max-file=3 \
    "${run_args[@]}" \
    "$image" >/dev/null
}

wait_healthy() {
  local url="http://127.0.0.1:${PORT}${HEALTH_PATH}"
  local deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    # 기동 중의 연결 거부·404 는 정상적인 대기 상황이라 curl 메시지를 숨긴다 (실패 시 컨테이너 로그를 따로 찍는다)
    if curl -fs -o /dev/null "$url"; then
      return 0
    fi
    # 컨테이너가 이미 죽었으면 더 기다리지 않는다
    if [ "$(docker inspect --format '{{.State.Running}}' "$APP_NAME" 2>/dev/null)" != "true" ]; then
      return 1
    fi
    sleep 2
  done
  return 1
}

# 4. 서버 교체
log "컨테이너 교체: $APP_NAME ← $IMAGE"
start_container "$IMAGE"

# 5. 헬스 확인, 실패 시 되돌리기
if wait_healthy; then
  log "헬스 확인 통과: ${HEALTH_PATH}"
else
  log "헬스 확인 실패. 컨테이너 로그(마지막 50줄):"
  docker logs --tail 50 "$APP_NAME" 2>&1 || true
  if [ -n "$previous_image" ] && [ "$previous_image" != "$IMAGE" ]; then
    log "직전 이미지로 되돌립니다: $previous_image"
    start_container "$previous_image"
    if wait_healthy; then
      log "되돌리기 완료 (직전 이미지로 동작 중)"
    else
      log "되돌리기도 헬스 확인에 실패했습니다. 수동 확인이 필요합니다"
    fi
  fi
  exit 1
fi

# 6. 옛 이미지 정리 (돌고 있는 컨테이너가 쓰는 이미지는 지워지지 않는다)
docker image prune -af --filter "until=24h" >/dev/null 2>&1 || true

log "배포 완료: $IMAGE"
