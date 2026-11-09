# CI/CD: 테스트 · 이미지 빌드 · dev 서버 배포

> 티켓 GLOW-142 (2026-11-09). 프론트(glowship-web) 배포는 그 저장소의 `docs/deploy.md`에 있습니다.
>
> 이 문서에는 계정 ID, 인스턴스 ID, 역할 ARN, DB 주소, 비밀값을 적지 않습니다. 실제 값은 AWS 콘솔과 GitHub 저장소 Settings에서 확인합니다.

**관련 파일**

| 파일 | 역할 |
| --- | --- |
| `.github/workflows/test.yml` | PR·push 때 pytest 실행 (CI) |
| `.github/workflows/deploy-dev.yml` | develop push 때 이미지 빌드 → ECR → EC2 배포 (CD) |
| `Dockerfile`, `.dockerignore` | FastAPI 서버 이미지 |
| `scripts/deploy/deploy-dev.sh` | EC2 안에서 실행되는 배포 스크립트 |
| `app/core/health.py` | 배포 확인용 `GET /health` |

---

## 1. 한눈에 보기

```mermaid
flowchart LR
    subgraph GH["GitHub"]
        DEV[개발자 push / PR] --> REPO[(glowship-server)]
        REPO --> GA[GitHub Actions]
    end

    subgraph AWS["AWS (서울 리전)"]
        IAM[IAM 역할<br/>OIDC 신뢰]
        ECR[(Amazon ECR<br/>이미지 저장소)]
        SSM[Systems Manager<br/>Run Command]

        subgraph VPC["VPC glowship-dev-vpc"]
            subgraph PRIV["사설 서브넷 (공인 IP 없음)"]
                EC2[EC2 glowship-backend<br/>Amazon Linux 2023 · arm64<br/>docker · FastAPI :8000]
                RDS[(RDS PostgreSQL 17<br/>glowship-dev-postgres)]
            end
            NAT[EC2 glowship-nat<br/>NAT 인스턴스]
        end

        CF[CloudFront<br/>drf9sclvif8bb.cloudfront.net]
        S3[(S3<br/>프론트 빌드 결과물)]
        SM[Secrets Manager<br/>RDS 마스터 비밀번호]
    end

    GA -- "① OIDC 토큰으로 역할 가정" --> IAM
    GA -- "② 이미지 push" --> ECR
    GA -- "③ deploy-dev.sh 전송" --> SSM
    SSM -- "명령 실행" --> EC2
    EC2 -- "이미지 pull (NAT 경유)" --> ECR
    EC2 -- "설정 파일 생성 때 1회" --> SM
    EC2 --> RDS

    USER((사용자 브라우저)) --> CF
    CF -- "기본 경로" --> S3
    CF -- "/api/* 등<br/>VPC 오리진" --> EC2
```

핵심 세 가지만 기억하면 됩니다.

1. **서버는 담장 안에 있다.** EC2는 사설 서브넷에 있어 밖에서 직접 닿을 수 없습니다. 들어오는 길은 CloudFront(사용자용)와 SSM(배포용) 둘뿐이고, SSH 포트도 공인 IP도 없습니다.
2. **비밀키를 저장소에 두지 않는다.** GitHub Actions는 OIDC 토큰으로 AWS 역할을 잠깐 빌려 씁니다. RDS 비밀번호는 Secrets Manager가 관리하고 EC2 안에서만 읽습니다.
3. **프론트와 API가 같은 주소다.** CloudFront 하나가 기본 경로는 S3(프론트)로, `/api/*`는 EC2(서버)로 보냅니다. 같은 출처라 쿠키 로그인이 그대로 동작합니다.

---

## 2. 쓰는 서비스와 역할

### 2.1 GitHub 쪽

| 서비스 | 무엇인가 | 여기서 하는 일 |
| --- | --- | --- |
| **GitHub Actions** | 저장소 이벤트(push, PR)에 반응해 가상 머신에서 작업을 실행하는 자동화 도구 | 린트, 테스트, 이미지 빌드, 배포 명령 전송 |
| **Secrets / Variables** | 저장소 Settings의 값 저장소. Secret은 가려지고 Variable은 보임 | 역할 ARN(Secret), 리전·ECR 이름·EC2 ID·플랫폼(Variables) |
| **Environment `dev`** | 배포 대상 환경 단위. 이력이 남고 승인자를 둘 수 있음 | 배포 job이 사용. 운영 환경을 만들 때 승인자를 붙이는 자리 |
| **OIDC 토큰** | Actions 실행마다 GitHub가 발급하는 짧은 신분증. "어느 저장소의 어느 브랜치에서 왔는지"가 적혀 있음 | AWS가 이 신분증을 믿고 역할을 빌려줌. Access Key를 저장할 필요가 없음 |

### 2.2 AWS 쪽

| 서비스 | 무엇인가 | 여기서 하는 일 |
| --- | --- | --- |
| **IAM (Identity and Access Management)** | 누가 무엇을 할 수 있는지 정하는 권한 체계 | OIDC 공급자 등록, Actions용 역할 `glowship-github-deploy`, EC2용 역할 `glowship-ec2-role` |
| **ECR (Elastic Container Registry)** | Docker 이미지 창고 | 저장소 `glowship-server`. 커밋 SHA 태그와 `dev-latest` 태그 |
| **EC2 (Elastic Compute Cloud)** | 가상 서버 | `glowship-backend`에서 docker로 FastAPI 컨테이너 실행. Graviton(arm64) t4g.small |
| **Systems Manager (SSM) Run Command** | 서버에 들어가지 않고 명령을 실행시키는 관리 서비스. 서버의 에이전트가 AWS로 먼저 연결을 맺어 둠 | 배포 스크립트 실행. 인바운드 포트를 열지 않아도 됨 |
| **RDS (Relational Database Service)** | 관리형 데이터베이스 | PostgreSQL 17. 사설 서브넷, SSL 강제 |
| **Secrets Manager** | 비밀값 금고 | RDS 마스터 비밀번호를 AWS가 직접 관리. 사람이 알 필요 없음 |
| **VPC / 서브넷 / 보안 그룹** | 가상 네트워크, 그 안의 구역, 구역별 방화벽 | EC2·RDS는 사설 서브넷. 보안 그룹으로 "누가 어느 포트로 들어올 수 있나"를 제한 |
| **NAT 인스턴스** | 사설 서브넷에서 인터넷으로 나가는 출구 | EC2가 ECR·Secrets Manager·패키지 저장소에 나갈 때 사용 |
| **CloudFront** | 전 세계 엣지에서 요청을 받아 뒤쪽으로 전달하는 CDN. HTTPS 주소를 무료로 제공 | 사용자 진입점. S3와 EC2 두 곳으로 경로별 분배 |
| **CloudFront VPC 오리진** | CloudFront가 사설 서브넷 안의 서버로 직접 연결하는 기능 | EC2를 공인 서브넷으로 옮기지 않고도 외부 접근 제공 |
| **S3 (Simple Storage Service)** | 파일 저장소 | 프론트 빌드 결과물. 비공개이며 CloudFront만 읽을 수 있음 |

---

## 3. CI: Test 워크플로 (`test.yml`)

```mermaid
flowchart LR
    T[main·develop push / PR] --> C[checkout]
    C --> P[(PostgreSQL 17<br/>서비스 컨테이너)]
    C --> U[uv sync --frozen<br/>Python 3.14]
    U --> M[alembic upgrade head]
    P --> M
    M --> PT[pytest]
```

- Ruff 검사는 기존 `code-style.yml`이 맡고, 이 워크플로는 pytest만 돌립니다.
- **DB를 띄우고 마이그레이션까지 적용**하는 이유: `tests/conftest.py`의 `db_session` 픽스처는 DB가 없거나 head가 아니면 건너뜁니다. 빈 DB라도 head 상태로 만들어 두면 저장소 SQL 스모크 시험이 실제로 돕니다.
- 설정은 `.env` 대신 워크플로 `env`에 넣습니다. DB 접속 정보, CI 전용 더미 JWT 키(32자 이상), 허용 오리진입니다. 저장소에 `.env`는 없습니다.
- 엔진(`GLOWSHIP_ENGINE_DIR`)은 저장소에 없어 CI는 엔진 없이 돌고, 엔진 대조 시험은 설계대로 건너뜁니다(`docs/analysis/engine-local-path.md`).
- **합성데이터가 있어야 하는 시험은 행이 없으면 건너뛰어야 합니다.** 빈 DB에서 `scalar_one()`처럼 행을 전제하면 CI가 실패합니다. 첫 실행에서 이런 시험 3건이 실패해 `pytest.skip`으로 고쳤습니다.

---

## 4. CD: Deploy Dev 워크플로 (`deploy-dev.yml`)

### 4.1 배포 흐름

```mermaid
sequenceDiagram
    autonumber
    participant Dev as 개발자
    participant GH as GitHub Actions
    participant IAM as AWS IAM (OIDC)
    participant ECR as Amazon ECR
    participant SSM as Systems Manager
    participant EC2 as EC2 glowship-backend
    participant RDS as RDS PostgreSQL

    Dev->>GH: develop 에 push (PR 머지)
    GH->>IAM: OIDC 토큰으로 역할 가정
    IAM-->>GH: 임시 자격 증명
    GH->>ECR: Dockerfile 빌드 (linux/arm64) → push<br/>태그: 커밋 SHA, dev-latest
    GH->>SSM: send-command (deploy-dev.sh 내용 + IMAGE)
    SSM->>EC2: 스크립트 실행 (root)
    EC2->>ECR: 이미지 pull
    EC2->>RDS: alembic upgrade head
    EC2->>EC2: 컨테이너 교체 → GET /health 대기
    alt 헬스 200
        EC2-->>SSM: 배포 완료 (Success)
    else 헬스 실패
        EC2->>EC2: 직전 이미지로 되돌림
        EC2-->>SSM: 종료 코드 1 (Failed)
    end
    SSM-->>GH: 결과 + 스크립트 로그
    GH-->>Dev: Actions 로그·Summary
```

### 4.2 두 job

| job | 하는 일 | 참고 |
| --- | --- | --- |
| **build-and-push** | OIDC 역할 가정 → ECR 로그인 → buildx로 빌드 → push | 플랫폼은 Variables `IMAGE_PLATFORM`(dev EC2는 Graviton이라 `linux/arm64`). 빌드 캐시는 Actions 캐시 |
| **deploy** (Environment `dev`) | `scripts/deploy/deploy-dev.sh` 내용을 jq로 SSM 파라미터에 담아 전송 → 완료 대기 → 스크립트 stdout·stderr를 로그에 펼침 → SSM 상태가 Success가 아니면 실패 | 스크립트를 EC2에 미리 복사해 둘 필요가 없음. 저장소가 유일한 원본 |

- develop push와 수동 실행(Run workflow)에서 돕니다. 배포끼리 겹치지 않게 직렬로 실행하고, 진행 중인 배포는 취소하지 않습니다.
- 테스트 통과와 무관하게 develop push 시 바로 배포합니다. develop은 PR 승인이 있어야 머지되고 PR 단계에서 Test가 돌기 때문입니다.

### 4.3 deploy-dev.sh (EC2 안에서 실행)

```mermaid
flowchart TD
    A[ECR 로그인<br/>EC2 인스턴스 역할 권한] --> B[새 이미지 pull]
    B --> C[현재 실행 중 이미지 기억<br/>되돌리기용]
    C --> D[같은 이미지로<br/>alembic upgrade head]
    D --> E[기존 컨테이너 내리고<br/>새 이미지로 기동]
    E --> F{GET /health<br/>200?}
    F -- 예 --> G[옛 이미지 정리<br/>배포 완료]
    F -- 아니오 --> H[컨테이너 로그 출력]
    H --> I[직전 이미지로 되돌림]
    I --> J[종료 코드 1]
```

- 설정은 EC2의 `/etc/glowship/server.env`(root 전용 600)에서 읽습니다. 키는 `.env.example`과 같고, 이 파일은 서버에만 있습니다.
- 되돌리기는 컨테이너만 바꿉니다. 이미 적용된 마이그레이션은 되돌리지 않습니다(downgrade는 수동 판단).
- 엔진 폴더가 EC2에 있으면 Variables `DEV_ENGINE_DIR`로 경로를 줍니다. 읽기 전용으로 마운트되고 `GLOWSHIP_ENGINE_DIR`이 설정됩니다. 비우면 엔진 없이 뜨고 분석 기능은 비활성입니다.
- 로컬에서도 돌려볼 수 있습니다. `ECR_LOGIN=0`이면 ECR 로그인을 건너뛰고 로컬 이미지를 그대로 씁니다.
  ```bash
  ECR_LOGIN=0 IMAGE=glowship-server:local ENV_FILE=<env 파일> PORT=18000 scripts/deploy/deploy-dev.sh
  ```

---

## 5. Dockerfile

```mermaid
flowchart LR
    subgraph B["builder (python:3.14-slim + uv)"]
        B1[pyproject.toml, uv.lock 복사] --> B2[uv sync --frozen --no-dev<br/>.venv 생성]
    end
    subgraph R["runtime (python:3.14-slim)"]
        R1[.venv 복사] --> R2[app, migrations, alembic.ini 복사]
        R2 --> R3[USER app · EXPOSE 8000<br/>uvicorn app.main:app]
    end
    B2 -.-> R1
```

- 2단계 빌드라 최종 이미지에는 uv와 빌드 캐시가 없습니다. 의존성 레이어는 `uv.lock`이 바뀔 때만 다시 만들어집니다.
- 비root 사용자 `app`으로 실행합니다.
- `.env`·`tests`·`docs`·`resource`·`scripts`는 `.dockerignore`로 빌드 컨텍스트에서 뺍니다. 설정은 환경변수로만 받습니다.
- 마이그레이션을 이미지에 포함해 배포 스크립트가 같은 이미지로 `alembic upgrade head`를 실행합니다.
- 엔진 코드는 이미지에 넣지 않습니다(공개 저장소·데이터 팀 결과물).
- 로컬 확인:
  ```bash
  docker build -t glowship-server:local .
  docker run --rm -p 8000:8000 --env-file .env -e POSTGRES_HOST=host.docker.internal glowship-server:local
  curl http://localhost:8000/health   # {"status":"ok","database":"ok"}
  ```

---

## 6. 사용자 요청 경로 (CloudFront)

```mermaid
flowchart LR
    U((브라우저)) -- HTTPS --> CF[CloudFront<br/>drf9sclvif8bb.cloudfront.net]
    CF -- "/ , /login , /assets/* …<br/>(기본 동작, 캐시 켬)" --> FN[CloudFront Function<br/>확장자 없는 경로 → /index.html]
    FN --> S3[(S3 프론트 버킷<br/>OAC 로 CloudFront 만 읽기)]
    CF -- "/api/*  /docs*  /openapi.json  /health<br/>(캐시 끔, 헤더·쿠키 그대로 전달)" --> VO[VPC 오리진]
    VO -- "보안 그룹: CloudFront 관리형 그룹 → 8000 만 허용" --> EC2[EC2 FastAPI :8000]
    EC2 --> RDS[(RDS)]
```

| 경로 | 가는 곳 | 캐시 |
| --- | --- | --- |
| `/api/*` | EC2 (FastAPI) | 끔. 쿠키·헤더·쿼리 전부 서버로 전달 |
| `/docs*`, `/openapi.json`, `/health` | EC2 | 끔. 개발 확인용 |
| 그 외 전부 | S3 (프론트 빌드) | 켬. `/login` 같은 SPA 경로는 Function이 `index.html`로 바꿈 |

- HTTP로 오면 HTTPS로 리다이렉트합니다. 도메인을 사지 않아도 CloudFront 기본 도메인으로 HTTPS가 됩니다.
- S3 버킷은 비공개이고 OAC(Origin Access Control)로 이 CloudFront 배포만 읽을 수 있습니다.
- backend 보안 그룹의 8000 포트는 CloudFront VPC 오리진 전용 보안 그룹에서만 열려 있습니다. 인터넷 전체에는 열려 있지 않습니다.

---

## 7. GET /health

- `app/core/health.py`. 서버가 요청을 받고 앱 DB 엔진으로 `SELECT 1`이 3초 안에 되면 200 `{"status":"ok","database":"ok"}`, 아니면 503 `{"status":"error","database":"error"}`.
- 인증 없음, `/api/v1` 밖, OpenAPI 문서에서 제외. 실패 사유는 응답에 넣지 않고 로그에만 남깁니다.
- 배포 스크립트의 헬스 확인과 외부 동작 확인(`https://<CloudFront 도메인>/health`)에 씁니다. 프론트 API 목록(`docs/frontend-api.md`)에는 넣지 않습니다.

---

## 8. 한 번만 하는 준비 (어디에 무엇이 있나)

### 8.1 GitHub 저장소 Settings

| 종류 | 이름 | 내용 |
| --- | --- | --- |
| Secret | `AWS_ROLE_ARN` | Actions가 가정할 IAM 역할 ARN |
| Variable | `AWS_REGION` | ECR·EC2 리전 |
| Variable | `ECR_REPOSITORY` | `glowship-server` |
| Variable | `DEV_EC2_INSTANCE_ID` | dev 서버 EC2 인스턴스 ID |
| Variable | `IMAGE_PLATFORM` | `linux/arm64` |
| Variable | `DEV_ENGINE_DIR` | 선택. EC2의 엔진 폴더 경로 |
| Environment | `dev` | 배포 job이 사용. 승인자를 두려면 여기서 설정 |

### 8.2 AWS 자원

| 자원 | 내용 |
| --- | --- |
| IAM OIDC 공급자 | `token.actions.githubusercontent.com`. 계정에 하나 |
| IAM 역할 `glowship-github-deploy` | Actions용. 신뢰 정책은 이 저장소의 `develop`(ref)과 `environment:dev` 토큰만 허용. 권한은 ECR 저장소 `glowship-server` push, dev EC2 한 대에 대한 `ssm:SendCommand`(문서 `AWS-RunShellScript`), `ssm:GetCommandInvocation` |
| ECR 저장소 `glowship-server` | 태그 가변(`dev-latest` 덮어쓰기), push 시 취약점 스캔 |
| EC2 인스턴스 역할 `glowship-ec2-role` | `AmazonSSMManagedInstanceCore`, `AmazonEC2ContainerRegistryReadOnly`, RDS 마스터 비밀번호(Secrets Manager) 읽기 |
| EC2 `glowship-backend` | Amazon Linux 2023 arm64. docker, aws cli, `/etc/glowship/server.env`. SSM으로 설치·작성 |
| RDS `glowship-dev-postgres` | PostgreSQL 17. 마스터 비밀번호는 Secrets Manager 관리형. SSL 강제(`rds.force_ssl=1`) |
| 보안 그룹 | backend: CloudFront VPC 오리진 관리형 그룹 → 8000. DB: backend → 5432 |
| CloudFront 배포 | VPC 오리진(EC2 8000, http-only) + S3 오리진(OAC). 경로별 동작은 6절 |
| CloudFront Function `glowship-dev-web-spa-router` | 확장자 없는 경로를 `/index.html`로 바꿈 (viewer-request) |
| S3 버킷 (프론트) | 비공개. 퍼블릭 접근 전부 차단. 버킷 정책으로 이 CloudFront 배포만 `GetObject` |

- 이 저장소의 GitHub OIDC 주체(sub)는 ID 포함 불변 형식(`repo:<조직>@<id>/<저장소>@<id>:...`)입니다. 신뢰 정책을 다시 쓸 때는 다음으로 확인합니다.
  ```bash
  gh api repos/<조직>/<저장소>/actions/oidc/customization/sub
  ```
- 같은 AWS 계정에 다른 프로젝트 자원이 있습니다. glowship 이름의 자원만 다룹니다.

### 8.3 dev 서버 설정 파일 (`/etc/glowship/server.env`)

- `APP_ENV=dev`, RDS 접속 정보(마스터 계정), `CORS_ORIGINS=["https://<CloudFront 도메인>"]`, 서버 안에서 생성한 `JWT_SECRET_KEY`.
- RDS 비밀번호는 EC2가 Secrets Manager에서 읽어 파일에 썼습니다. 사람 손을 거치지 않았고 로그에도 남기지 않았습니다.
- dev는 데모(합성데이터) 환경이라 `VIRTUAL_CLOCK_START`는 기본값을 씁니다. 실제 원천을 붙이는 환경에서는 비웁니다(`.env.example`).
- 역할 분리(`POSTGRES_ROLE`, 워커 계정)는 dev에서 쓰지 않습니다. 운영 단계에서 도입합니다.

---

## 9. 운영 절차

| 상황 | 하는 일 |
| --- | --- |
| **배포** | PR을 develop에 머지하면 자동. 수동은 Actions → Deploy Dev → Run workflow |
| **확인** | Actions 로그의 deploy job에 `deploy-dev.sh` 출력이 펼쳐져 있음. `배포 완료`가 찍히면 `https://<CloudFront 도메인>/health`가 200 |
| **헬스 실패** | 스크립트가 직전 이미지로 되돌림. 로그의 컨테이너 마지막 50줄을 먼저 봄 |
| **권한 오류** | ECR·SSM 거절은 IAM 역할 `glowship-github-deploy`의 정책, pull 거절은 `glowship-ec2-role` |
| **빌드 실패** | `Dockerfile`, `uv.lock`. 로컬 `docker build`로 재현 |
| **롤백** | 커밋 SHA 태그가 ECR에 남아 있음. EC2에서 `IMAGE=<ECR 주소>:<이전 SHA> AWS_REGION=<리전> scripts/deploy/deploy-dev.sh`를 SSM으로 실행 |
| **설정 변경** | `/etc/glowship/server.env`를 SSM으로 고친 뒤 재배포(또는 컨테이너 재시작). 비밀값은 출력에 찍지 않음 |

---

## 10. 아직 안 한 것

- dev DB에 합성데이터 적재(seed)와 관리자 계정 생성. 배포 뒤 로그인까지 확인하려면 필요합니다.
- 엔진 폴더를 EC2에 두고 `DEV_ENGINE_DIR` 설정(분석 기능 활성). 데이터 팀과 전달 방식 합의가 필요합니다.
- 운영 배포(main → 운영 서버) 워크플로. dev와 같은 모양에 Environment 승인자를 더하는 것을 권장합니다.
- Docker 이미지 크기(약 460MB) 축소, CloudFront 접근 로그.
