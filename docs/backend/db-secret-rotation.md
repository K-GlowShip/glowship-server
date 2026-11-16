# RDS 비밀번호 자동 교체와 서버 연결

## 문제와 변경

RDS 관리형 마스터 비밀번호는 Secrets Manager에서 교체되지만, 기존 서버는
`server.env`에 복사한 비밀번호를 계속 사용했다. 이미 맺은 연결은 살아 있어
`/health`가 정상이어도, 새 연결을 만드는 배포 전 마이그레이션은 인증에서 실패할 수 있다.

`POSTGRES_SECRET_ARN`을 넣으면 앱과 Alembic이 같은 연결 생성기를 사용한다.
새 물리 연결을 만들 때 `AWSCURRENT`의 비밀번호를 읽는다. 프로세스 메모리에 최대
5분 캐시하며, 캐시된 비밀번호가 `InvalidPasswordError`로 거절되면 즉시 다시 읽고
연결만 한 번 더 시도한다. SQL·트랜잭션·마이그레이션을 재실행하는 기능은 아니다.
AWS 조회는 별도 스레드에서 실행해 이벤트 루프를 막지 않는다.

서버 파일·이미지·명령 인자·로그에는 읽어 온 비밀번호를 쓰지 않는다.
Secrets Manager 조회 실패나 대상 계정 불일치는 고정 오류 코드로 종료하며,
오래된 `POSTGRES_PASSWORD`로 조용히 되돌아가지 않는다. 이 모드에서는
`POSTGRES_PASSWORD`를 읽지 않고 DB URL에도 넣지 않는다.

시드 스크립트(`scripts/seed_demo_runs.py`, `scripts/seed_synthetic_data.py`)도 같은 방식으로
붙는다. 이 모드에서는 `Settings.database_url`에 비밀번호가 없으므로, 스크립트는
`create_async_engine(database_url)`을 직접 부르지 않고 `create_database_engine`
(`app/core/db_secrets.py`)을 쓴다. ARN이 없으면 기존처럼 URL의 비밀번호로 붙는다.
새 스크립트가 직접 엔진을 만들면 `tests/core/test_db_secrets.py`가 실패한다.

## 적용 전 조건

- EC2 인스턴스 역할이 **대상 Secret 하나**의 `secretsmanager:GetSecretValue`를 허용해야 한다.
  관리형 기본 KMS 키가 아닌 고객 관리 키라면 해당 키 복호화 권한도 별도 검토한다.
- 컨테이너에서 IMDSv2로 인스턴스 역할을 사용할 수 있어야 한다.
  장기 AWS 액세스 키를 이미지나 `server.env`에 넣지 않는다.
- private EC2에서 Secrets Manager HTTPS에 도달해야 한다. 기존 NAT instance 경로를 사용한다.
  이 변경을 위해 NAT Gateway나 새 유료 엔드포인트를 만들지 않는다.
- `POSTGRES_USER`는 Secret의 `username`과 같아야 한다. Secret에 host/port/dbname이
  들어 있다면 서버 설정의 대상과도 같아야 한다.
- 이 변경은 DB 사용자·역할·RLS·자동 분석·실제 LLM을 켜거나 바꾸지 않는다.
  별도 워커 계정은 기존 설정을 그대로 사용한다. 별도 워커의 비밀번호 교체는 별도 작업이다.

## 배포 순서

1. 두 명 승인 등 저장소 규칙에 따라 이 변경을 리뷰·병합한다.
2. 비밀번호 갱신 기능을 포함하는 이미지가 준비된 뒤 `/etc/glowship/server.env`에
   다음 **비밀값이 아닌 식별자**를 넣는다. 공개 예시에 실제 ARN을 적지 않는다.

   ```dotenv
   POSTGRES_SECRET_ARN=arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:example-abcdef
   ```

3. 기존 배포 스크립트로 재배포한다. Alembic도 같은 설정을 읽으므로 별도로 비밀번호를
   복사하는 단계는 없다. 마이그레이션 성공 전에는 실행 중인 컨테이너를 교체하지 않는다.
4. `/health` 200과 신규 DB 연결 성공을 확인한다. 앱 연결·역할이 의도대로인지 확인한다.
5. 구판으로 되돌릴 필요가 없어진 뒤 기존 `POSTGRES_PASSWORD` 항목을 제거한다.
   구판 이미지는 Secret 모드를 모르므로 **오래된 비밀번호를 가진 구판으로는 롤백할 수 없다**.
   구판 롤백이 꼭 필요하면 운영 담당자가 현재 비밀번호를 안전하게 반영해야 한다.

초기 배포 전에 Secret을 적용한 새 이미지에서 읽기 전용 `SELECT 1`을 먼저 확인하면
기존 서버를 유지한 채 IAM·통신·인증 오류를 구분할 수 있다. 자동 분석 활성화는 별도 순서다.

## 검증과 한계

- 자동 시험: 캐시 만료, 동시 인증 실패 시 갱신 한 번, 교체된 비밀번호로 연결 재시도,
  재시도 상한, 네트워크 오류에서 불필요한 갱신 없음, 잘못된 Secret/대상 거부,
  비밀값 오류 노출 방지, 앱 역할 유지, URL 특수문자 처리.
- 실제 AWS 검증은 별도: 설치된 이미지에서 신규 연결, `/health`, 마이그레이션,
  그리고 자연스러운 다음 교체 뒤 신규 연결을 확인해야 한다.
- 검증 목적으로 운영 비밀번호를 강제 교체하지 않는다.
- 관리형 Secret 교체 주기는 유지한다. 조회 비용은 캐시 갱신·인증 실패 시에 발생하며,
  추가 EC2·프록시·엔드포인트 자원은 만들지 않는다.

공식 근거:
- <https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/rds-secrets-manager.html>
- <https://docs.sqlalchemy.org/en/21/orm/extensions/asyncio.html#sqlalchemy.ext.asyncio.create_async_engine>
