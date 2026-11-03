# 분석 실행 상세 응답 스키마

`GET /api/v1/analysis-runs/{runId}`(API 명세 v6.2 4.5절)의 응답 모양을 Pydantic 모델로 정의한 내용과, **왜 이렇게 만들었는지**를 설명합니다.
이번 작업 범위는 응답 스키마와 코드값 열거형까지입니다. 라우터·서비스·DB 조회는 아직 없습니다(6절). 서버의 분석 표는 마이그레이션 `0005`에서 v6.2 DDL과 같아졌고(`analysis_runs`와 분석 결과 표 4개, ORM 모델 포함), 조회는 다음 작업에서 이 스키마에 연결합니다.

작업 중 실패한 것과 그 원인은 8절에 빠짐없이 남겼습니다.

## 1. 근거 자료

| 자료 | 쓰는 곳 |
| --- | --- |
| API 명세 v6.2 4.5절 `GET /analysis-runs/{runId}` | 응답 필드 이름·묶음, 응답에 넣지 않는 것 |
| 명세 D2·D20·D21·D23 | camelCase, 저장 JSON 키 유지, 위험 주문 주문 단위, STALE 판정 방식 |
| ERD v6.2 DDL `erd_glowship_postgres_v6_2_analysis_ddl.sql` | 코드값(CHECK 제약), NULL 허용 여부 |
| 설명 스키마 `explanation.v1.schema.json` | AI 설명 구조 |
| 분석 JSON 계약 v3 1절 `sku_results.v1` | 대응안 SKU별 결과 구조 |
| 2026 H1 합성 데이터 overlay `v1_run1` | 실제 값이 모델로 읽히는지 확인 |

## 2. 응답 구조

```
AnalysisRunDetailResponse                 실행·STALE·AI 설명·실패 요약 필드는 최상위(명세 필드 이름 그대로)
├── target: AnalysisTarget                대상 지연 (선적·경로·단계·계획/예상 입고일)
├── inputBasis: InputBasis                입력 기준 (input_seq, 기준일, 운영 기준, 계산 계약 버전, 사용한 수집)
│   ├── dataVersions[]: DataVersion
│   └── dataWarnings[]: DataWarning
├── inventoryImpacts[]: InventoryImpact   SKU·창고별 재고 영향 (기준선 / 전체)
├── atRiskOrders[]: AtRiskOrder           위험 주문 (주문 단위로 묶음)
│   └── lines[]: AtRiskOrderLine          주문 상세 = at_risk_orders 1행
├── responseOptions[]: ResponseOption     대응안 비교
│   ├── skuResults: SkuResults            ← 저장 JSON, snake_case 그대로 (sku_results.v1)
│   └── outcomes[]: OptionOrderOutcome    대응안별 주문 상세 결과
├── explanation: Explanation              ← 저장 JSON, snake_case 그대로 (explanation.v1)
├── drafts[]: MemberDraftSummary
└── finalDecision: FinalDecisionSummary
```

| 파일 | 내용 |
| --- | --- |
| `app/modules/delay_analysis/infrastructure/analysis_enums.py` | 코드값 열거형 25종 (DDL CHECK·설명 스키마·JSON 계약과 같은 값) |
| `app/modules/delay_analysis/presentation/analysis_run_schemas.py` | 응답 모델 |
| `tests/modules/delay_analysis/test_analysis_run_schemas.py` | 단위 테스트 17개 함수, 매개변수 포함 21건 |

## 3. 왜 이렇게 만들었나

| 결정 | 내용 | 이유 |
| --- | --- | --- |
| 응답 본체는 `CamelModel` | 응답 필드는 camelCase | 명세 D2, 기존 공통 베이스(`app/core/schemas.py`)를 그대로 써서 다른 API와 표기 방식을 맞춤 |
| 저장 JSON 두 개는 `CamelModel`을 쓰지 않음 | `Explanation`·`SkuResults`는 `StoredJsonModel`(별칭 없음, `extra="forbid"`) | 명세 D20: 데이터 팀 계약이 키를 snake_case로 고정하고 모르는 키를 금지함. 설명은 근거 경로(JSON Pointer, 예: `/run/planned_3pl_inbound_date`)까지 검증하므로 키를 바꾸면 근거 경로와 어긋남 |
| 저장 JSON의 식별자는 `uuid.UUID`가 아니라 소문자·하이픈 문자열(`StoredUuid`) | 패턴 `^[0-9a-f]{8}-…$` | `uuid.UUID`는 대문자·하이픈 없는 표기도 받아들이고 출력할 때 바꿔 씀. "저장된 그대로"(D20)와 계약 0절 식별자 규칙에 어긋남(8절 F4) |
| raw 값은 소수 4자리 문자열(`RawDecimal`) | 패턴 `^[0-9]+\.[0-9]{4}$` | 계약 0절: 정수화 경계 재현 때문에 부동소수 금지 |
| `3pl` 필드는 별칭을 직접 지정 | `planned3plInboundDate`, `expected3plInboundDate`, `baselinePlanned3plInboundDate` | `to_camel`이 `planned3PlInboundDate`(P 대문자)로 바꿈. 명세 표기와 다름(8절 F1) |
| 코드값은 `StrEnum` | DDL CHECK 값과 1:1 | 같은 모듈의 `state.py`(`AnalysisStep`)와 같은 방식. 옛 v5 값(`EAST_FULFILL`, `PARTIAL`)이 들어오면 거부됨 |
| 열거형 파일은 `infrastructure/`에 둠 | `analysis_enums.py` | DB CHECK 값과 1:1인 코드값이라 ORM 모델 옆에 둠. 후속 PR에서 ORM 모델·조회 서비스·응답 스키마가 같은 열거형을 씀. 기존 `user/infrastructure/user_enums.py`·`company/infrastructure/company_enums.py`와 같은 위치이고, presentation이 이를 가져다 쓰는 것도 `client_account_schemas.py`와 같음. `AnalysisStep`은 DB 코드값이 아니라 에이전트 진행 단계라 `application/agent/state.py`에 있음 |
| 저장 JSON의 정수는 `StrictInt`, 근거 값은 `StrictStr \| StrictInt \| None` | `retry_no`, SKU별 결과·공여 창고의 수량, `evidence_references[].value` | 계약 0절 "정수는 JSON 정수", `explanation.v1`의 `value` 타입은 string·integer·null. 일반 `int`는 `True`를 `1`로, `"7"`을 `7`로 바꿔 받아 "저장된 그대로"(D20)가 깨짐(8절 F10) |
| `averageDailySales`는 JSON에서 숫자 | 서버 안에서는 `Decimal`, JSON으로 낼 때만 float(`JsonNumberDecimal`) | Pydantic 기본은 `Decimal`을 문자열(`"1.2500"`)로 내보냄. 프론트가 표시·정렬·비교에 숫자를 씀. 명세에 타입이 없고 DDL은 `numeric`. 2026 H1 데이터 391건(소수 최대 6자리)은 숫자로 바꿔도 값이 같음(8절 F12) |
| STALE 사유를 열거형 두 개로 나눔 | `StaleReason`(대표 사유, ck28), `StaleReasonCode`(배열 항목, ck30) | `MULTIPLE_RELEVANT_CHANGES`는 대표 사유에만 쓰고 배열에는 넣지 않음(DDL ck30·ck31)(8절 F4) |
| 대응안 주문 집계는 `int \| None` | `fullyRecoveredOrders` 등 4개 | 집계 NULL 규칙 G-1~G-3: 전체 판정 불가·사전 조건 실패면 NULL. 0과 "계산 안 함"을 구분해야 함 |
| SKU별 결과의 원값·수량은 모두 null 허용 | `raw_*`, `*_quantity` | 계약은 "EVALUATED면 not null"만 정함. 실제 데이터에는 NOT_ESTIMABLE 항목에도 원값이 채워져 있음(3건). 둘 다 읽을 수 있어야 함 |
| `impact_source`만 키가 없어도 됨 | 기본값 `None` | 계약 v3 0.1절: v1·v2 엔진 산출물에는 키가 없고 "기록 없음"으로 읽음. 다른 키는 "키 생략 금지"(계약 0절) |
| 위험 주문을 주문으로 묶음 | `AtRiskOrder.lines[]` | 명세 D21: `at_risk_orders`는 주문 상세 1행. 화면 "위험 주문 N건"은 주문 수 |
| 기준일은 `date \| None` | `inputBasis.baseDate` | `analysis_runs`에 열이 없고 `dependency_scope.base_date`에만 있음. `ON_HOLD`·`FAILED`는 `dependency_scope`가 NULL(DDL ck36). 진행 중에 STALE이 된 실행도 의존 범위를 저장하기 전이라 NULL(5절) |
| `runCode`는 `str \| None` | | 명세 D14: v6.2에도 `run_code` 열이 없음. 열이 생기기 전까지 null |
| 응답에서 뺀 DB 열 | `analysis_runs.dependency_scope`·`decision_signature`·`decision_signature_sha256`, `inventory_impact_results.raw_*_shortage_quantity` | 명세 4.5 "응답에 넣지 않는 것"(428줄): 서버 내부 STALE 판정·검증용. 단, `explanation.v1` 안의 `decision_signature_hash`·`prompt_template_sha256`은 계약의 필수 메타데이터라 그대로 나감. 명세 4.5 AI 설명 표(482줄)가 이 메타데이터를 "표시하지 않음"(화면에 표시하지 않음)으로 두고, D20이 설명을 저장된 그대로 내리게 함. 판정 서명 본문(`decision_signature`)과 의존 범위는 나가지 않음(8절 F9) |
| `stage`·`sourceDomain`은 `str` | 열거형을 만들지 않음 | 선적·수집 코드값은 다른 영역(B) 모듈이 소유. 이 모듈에서 따로 정의하면 두 곳을 맞춰야 함 |

### 명세에 이름이 없어서 정한 것

| 이름 | 명세 표현 | 이유 |
| --- | --- | --- |
| `target`, `inputBasis`, `inventoryImpacts`, `atRiskOrders`, `responseOptions` | 4.5 표의 "묶음" 칸(대상, 입력 기준, 재고 영향, 위험 주문, 대응안 비교) | 묶음 이름이 명세에 없음. 프론트·A·B와 맞출 필요가 있음 |
| `inputBasis.operatingRuleVersionNo` | "`operatingRuleSetId`·`versionNo`" | 최상위 실행의 `versionNo`(분석 버전)와 이름이 겹쳐서 구분 |
| `baseDate` | "기준일 D" | |

### 명세보다 더 넣은 필드

| 필드 | 이유 |
| --- | --- |
| `AtRiskOrder.orderId`, `AtRiskOrderLine.skuCode` | 주문 묶음의 식별자, 화면 표시용 SKU 코드 |
| `InventoryImpact.skuCode`·`productName`·`warehouseCode` | 명세는 "SKU·창고별"로만 적음. 화면에 ID만으로는 표시할 수 없음 |
| `DataVersion.dataSyncRunId`·`finishedAt` | "사용한 수집 목록"의 내용 |
| `drafts[].memberDraftId`·`displayName`, `finalDecision.finalDecisionId`·`displayName` | 명세의 "작성자"·"선택자"를 화면에 이름으로 표시 |

## 4. 저장 JSON과 응답의 경계

```json
{
  "runId": "…",
  "target": { "planned3plInboundDate": "2026-02-11" },
  "responseOptions": [
    {
      "optionType": "OTHER_REGION_FULFILL",
      "skuResults": {
        "schema_version": "sku_results.v1",
        "items": [{ "sku_id": "…", "raw_increment_shortage": "6.5714" }]
      }
    }
  ],
  "explanation": { "schema_version": "explanation.v1", "run_summary": "…" }
}
```

감싸는 필드 이름(`skuResults`, `explanation`)만 camelCase이고, 그 안은 저장된 키 그대로입니다(D20).

## 5. 상태별로 비는 값

명세 4.5 "결과 분류와 상태별로 보이는 내용"을 따릅니다. 모델은 아래 경우를 모두 받아들이도록 null·빈 목록을 허용합니다. 어떤 상태에 무엇을 채울지는 서비스(조회 구현)가 정합니다.

| 상태 | 결과 목록 | `explanation` | 실패 요약 | `stale*` |
| --- | --- | --- | --- | --- |
| `READY_FOR_REVIEW`·`FINALIZED` | 있음 | 있음(검증 통과 시) | null | null |
| `NO_RISK` | 재고 영향만 | null | null | null |
| `NO_DELAY`·`ON_HOLD`·도구 실패 `FAILED` | 빈 목록 | null | `ON_HOLD`·`FAILED`만 있음 | null |
| `FAILED`(`EXPLANATION_GENERATION_FAILED`) | 있음 | null | 있음 | null |
| `STALE`(완료 뒤에 STALE) | 완료 당시 값 | 완료 당시 값 | null | 있음 |
| `STALE`(진행 중에 STALE) | 빈 목록 | null | null | 있음 |

진행 중(`DETECTED`~`VALIDATING_OUTPUT`)인 실행도 수집 적재 때 STALE이 됩니다. develop `stale_service.py`의 STALE 대상(`STALE_TARGET_STATUSES`)에 진행 중 상태가 들어 있고, 의존 범위 없이 판정합니다. 이 실행은 `completedStatus`·`resultClass`·`inputBasis.baseDate`도 null입니다(DDL ck25는 STALE이면서 `completed_status`가 없는 경우를 허용).

## 6. 아직 없는 것

| 항목 | 상태 |
| --- | --- |
| 라우터·서비스·DB 조회 | 없음. 분석 표 5개와 ORM 모델은 Alembic `0005`에서 v6.2 DDL과 같아짐(`0003`은 STALE 사유 4열·`dependency_scope`만, `0005`가 실행 사유·완료 상태·결과 분류·판정 서명과 결과 표 4개). 조회 구현 작업에서 연결 |
| ORM 행 → 응답 변환 | 없음. 위험 주문을 주문으로 묶는 것, `dependency_scope.base_date` 읽기, route·대표 상품(D13, B의 계산 함수)은 조회 구현에서 함 |
| `runCode` | null. `run_code` 열 추가(D14, Alembic `0006`) 뒤에 채움 |
| 공통 camelCase 변환의 `3pl` 표기 | 이 스키마만 별칭으로 고침. 같은 필드를 쓰는 다른 API(지연 목록·선적)도 같은 문제가 있어 공통 베이스 담당과 공유 필요 |

## 7. 검증

| 검사 | 방법 | 결과 |
| --- | --- | --- |
| 명세 4.5 필드 | 명세 표의 백틱 필드 111개 + 한글 항목 대응을 모델 출력 키(별칭)와 묶음별 비교 | 빠진 필드 없음 |
| 코드값 | 열거형 25종: 24종은 DDL CHECK·설명 스키마 enum·계약 v3 값과 집합 비교, `StaleReasonCode`는 DDL ck30 배열 허용값과 비교. 같은 값을 쓰는 다른 자리 2곳(설명 `option_type`, 계약 `sku_scope`) | 일치 (처음엔 2건 실패, 8절 F4) |
| NULL 허용 | DDL 열과 이름이 같은 필드 99개: NOT NULL ⇔ 모델 필수 | 일치 |
| explanation.v1 | 필수 키, 모든 패턴·길이·개수 제한, 모르는 키 금지 | 일치 (처음엔 1건 실패, 8절 F4) |
| sku_results.v1 | 항목 키 16개, donor 키 13개, 최상위 4개 | 일치 |
| 실데이터 | overlay의 설명 13, 실행 코드값 21, 대응안 39, 주문 결과 21, 재고 영향 391, 초안 2, 최종 선택 2 | 전부 읽힘 |
| 검사가 틀린 것을 잡는지 | 옛 `EAST_FULFILL` 추가, NOT NULL 열을 null 허용으로, null 열을 필수로, 설명 길이 600→900, 명세 필드 하나 삭제 | 5건 모두 잡음 |
| 테스트가 틀린 것을 잡는지 | `3pl` 별칭을 지우고 실행 | 1건 실패 확인 후 복원(해시 같음) |
| 리뷰 반영(PR #8) | 새 테스트 5건(근거 값 불리언, SKU 정수 불리언·문자열, 평균 판매량 숫자, 설명 메타데이터 유지)을 고치기 전 코드에서 먼저 실행 → 메타데이터 유지(현재 동작 확인용)를 뺀 4건 실패 확인. 내부 열 검사에 snake·camel 이름과 묶음 안 이름을 주입 → 3건 모두 잡음. overlay 설명 13·SKU별 결과 39가 `StrictInt`로 읽히고, 평균 판매량 391건은 숫자로 바꿔도 값이 같음 | 통과 |
| 린트·테스트 | `uv run ruff check . && uv run ruff format --check . && uv run pytest` | 통과. 이 문서의 테스트 21건, 전체 368건 (develop `2f651d6` + `0005` + 이 후속 수정 기준, 건너뛴 테스트 없음. 후속 수정을 처음 쓴 `e96db1f` 기준 93건, PR #8 머지 때 92건, 첫 작성 때 53건) |

**검증의 한계**

- 한글로만 적힌 명세 항목(선적 코드, 기준일 D, 작성자 등)은 사람이 대응시켰습니다.
- NULL 허용 대조는 DDL 열과 이름이 같은 필드만 했습니다. `target`·`inputBasis`처럼 다른 테이블에서 계산해 오는 값은 대조하지 않았습니다.
- overlay 데이터에는 `READY_ORDER_RISK`, NULL 주문 집계, `impact_source` 없는 산출물이 0건이라, 이 경우는 단위 테스트의 직접 만든 값으로만 확인했습니다.
- 위험 주문의 주문 단위 값(귀속·배송일·사유)이 같은 주문의 상세끼리 같다는 것은 overlay 주문 3건에서만 확인했습니다. DDL이 보장하지는 않습니다.

## 8. 작업 중 실패와 원인

| # | 실패 | 원인 | 조치 |
| --- | --- | --- | --- |
| F1 | 첫 판의 `3pl` 필드가 `planned3PlInboundDate`로 나감 | 공통 `CamelModel`의 `to_camel`이 숫자 뒤 글자를 대문자로 바꾸는 것을 확인하지 않고 그대로 씀 | 별칭 지정. 출력 키를 명세 표기와 비교하는 테스트 추가 |
| F2 | 처음 계획에서 "기준일 D"를 `analysis_runs`의 열로 가정 | 명세 표만 보고 DDL을 확인하지 않음 | DDL·엔진 확인 결과 `dependency_scope.base_date`에만 있음. `date \| None`으로 정의 |
| F3 | `ruff format --check` 실패(새 파일 2개) | 긴 줄을 포맷하지 않고 작성 | `ruff format` 적용 |
| F4 | 명세·DDL 대조에서 스키마 문제 2건 발견: (1) `staleReasonCodes`가 `MULTIPLE_RELEVANT_CHANGES`를 허용 (2) 저장 JSON 식별자가 `uuid.UUID`라 다른 표기를 받고 출력 때 바꿈 | (1) 대표 사유와 배열 항목의 허용값이 다른 것(ck28과 ck30)을 놓치고 열거형 하나를 같이 씀 (2) 일반 응답 필드처럼 `uuid.UUID`를 습관적으로 씀 | `StaleReasonCode` 분리, `StoredUuid` 도입, 각각 테스트 추가 |
| F5 | 첫 보고에서 "검증 통과"라고 했지만 F4를 놓침 | 실데이터 읽기와 단위 테스트만 했고, 명세·DDL과 필드·값을 하나씩 대조하지 않음. 실데이터에 해당 경우(`MULTIPLE_RELEVANT_CHANGES`, 다른 UUID 표기)가 없어서 드러나지 않음 | 7절 대조 검사를 추가해 다시 검증 |
| F6 | 대조 검사 스크립트 자체의 버그 3건: (1) DDL `ck30`을 못 찾아 중단 (2) `ck6`의 `feasibility_status <> 'NOT_FEASIBLE'`까지 허용값으로 읽음 (3) 계약 표의 `\|`(칸 안 값 구분)를 칸 구분으로 잘라 첫 값만 읽음 | (1) 제약 끝이 `]::text[]),`인 경우를 고려하지 않은 정규식 (2) 조건식 전체에서 값을 뽑음 (3) 마크다운 이스케이프를 고려하지 않음 | 줄 단위 정규식, 마지막 `IN (...)` 목록만 읽기, `\|` 치환 후 분리. 고친 뒤 음성 시험 5건으로 검사가 실제로 잡는지 확인 |
| F7 | 대조 검사에 필요 없는 "의도적 예외"(`stale_reason_codes`)를 넣음 | NULL 허용이 실제로 다른지 확인하지 않고 예외로 둠 | 예외 삭제 후 재실행, 99개 모두 일치 |
| F8 | 첫 커밋 4개의 제목 줄에 본문 불릿이 전부 붙음 | 커밋 메시지에서 제목과 본문 사이 빈 줄을 빠뜨림. git은 첫 빈 줄 전까지를 제목으로 읽음 | push 전이라 `git reset --mixed develop`으로 커밋만 되돌리고(파일 유지) 같은 단위로 다시 커밋 |
| F9 | (리뷰) 문서·주석에 "decision_signature(·sha256)를 응답에서 뺐다"고 썼는데, 응답 `explanation` 안에 `decision_signature_hash`가 나감. 내부 필드 테스트도 camelCase 이름만 찾아 snake_case 키를 못 잡음 | "응답에서 뺀 것"이 DB 열이라는 범위를 적지 않음. 테스트는 응답 본체가 camelCase라는 가정만 하고, 저장 JSON(snake_case)을 고려하지 않음 | 문서·주석에 "DB 열"이라는 범위와 설명 메타데이터를 남기는 근거(명세 482줄, D20)를 적음. 테스트를 둘로 나눔: 저장 JSON을 뺀 나머지에서 snake·camel 이름을 모두 찾는 테스트, 설명 메타데이터가 남는지 확인하는 테스트 |
| F10 | (리뷰) 근거 `value`에 `True`를 넣으면 `1`로 바뀜 | `str \| int`의 `int`가 `True`를 받아들임(bool은 int의 하위 타입). 7절 explanation.v1 대조는 패턴·길이·개수만 비교하고 타입(`string·integer·null`)은 비교하지 않음 | `StrictStr \| StrictInt \| None`. 같은 원칙으로 저장 JSON의 정수 필드를 모두 `StrictInt`로 바꿈 |
| F11 | (리뷰) 7절 "열거형 24종"이 2절 "25종"과 다르고, "전체 53건"이 rebase 뒤 실제(58건)와 다름 | 24종은 "기준과 직접 비교한 수"였는데 설명 없이 적음. 테스트 수는 develop 변경 뒤 다시 세지 않음 | 25종 안의 비교 방식을 나눠 적고, 테스트 수는 기준 커밋과 함께 다시 센 값으로 적음 |
| F12 | (리뷰) `averageDailySales`가 JSON에서 문자열로 나감 | Pydantic이 `Decimal`을 문자열로 직렬화하는 것을 프론트 쓰임새와 함께 고려하지 않음 | JSON에서만 숫자로 내보내는 `JsonNumberDecimal` |
| F13 | (리뷰) 5절이 STALE을 "완료 당시 값"으로만 적고, 3절·`baseDate` 설명이 비는 경우를 `ON_HOLD`·`FAILED`로만 적음. develop(PR #9)은 진행 중 실행도 STALE로 바꾸고, 이때 `dependency_scope`·결과·설명이 없음 | develop(PR #9)을 merge할 때 테스트 수만 고치고, PR #9의 동작(진행 중 실행도 STALE 대상)이 문서와 맞는지 다시 보지 않음 | 5절에 "진행 중에 STALE" 줄과 설명 추가, 3절·`baseDate` 설명·결과 목록 주석에 추가, 응답 모양을 고정하는 테스트 추가 |
| F14 | (리뷰) 첫머리·6절의 "서버 분석 테이블이 v5 구조(Alembic `0001`)"가 develop과 다름. develop에는 `0002`·`0003`이 있고 `0003`이 STALE 사유 4열·`dependency_scope`를 추가함 | F13과 같은 원인. merge 뒤 문서 표현을 다시 확인하지 않음 | `0003`까지 반영된 상태와 아직 남은 v6.2 전환 범위로 고쳐 씀(F15에서 다시 고침) |
| F15 | F13·F14 수정을 11-02에 `0003` 기준으로 써 두고 push하지 않은 사이, develop에 `0004`가 들어오고 이 PR의 `0005`가 분석 표를 v6.2로 바꿈. "0003까지, 나머지는 다음 리비전"이 다시 사실과 달라짐 | 리뷰어 요청대로 DB 연결 PR(`0005`)에 함께 넣으면서, 써 둔 문장을 그 PR 기준으로 다시 확인하지 않으면 같은 문제가 반복됨 | 첫머리·6절을 `0005` 상태로, `runCode`를 `0006`으로, 테스트 수를 다시 세어 고쳐 씀 |

## 9. 테스트

`tests/modules/delay_analysis/test_analysis_run_schemas.py` (DB 없이 실행)

| 테스트 | 확인 내용 |
| --- | --- |
| `test_top_level_fields_follow_spec_camel_case` | 최상위 키가 명세 4.5 필드 + 묶음 5개와 정확히 같음 |
| `test_3pl_dates_use_spec_spelling` | `3pl` 필드가 명세 표기, `3Pl` 없음 |
| `test_stored_json_keeps_snake_case_keys` | 설명·SKU별 결과 안의 키가 저장된 그대로 |
| `test_response_does_not_expose_internal_columns` | 저장 JSON을 뺀 응답에 내부 DB 열 이름(snake·camel 모두)이 없음 |
| `test_explanation_keeps_contract_metadata` | 설명 안의 `decision_signature_hash`·`prompt_template_sha256`은 저장된 그대로 나감 |
| `test_explanation_rejects_unknown_key` | 설명에 `delay_cause` 같은 모르는 키 거부 |
| `test_stored_json_ids_must_be_lowercase_hyphenated` | 대문자·하이픈 없는 UUID 거부 |
| `test_sku_results_reads_output_without_impact_source` | 옛 엔진 산출물(키 없음) 읽힘 |
| `test_explanation_evidence_rejects_bool_value` | 근거 값 `True` 거부 (1로 바꾸지 않음) |
| `test_sku_results_integers_are_not_coerced` | SKU별 결과 정수에 불리언·문자열 거부 |
| `test_sku_results_rejects_non_contract_raw_value` | 부동소수·자릿수 다른 raw 값 거부 |
| `test_needs_check_option_allows_null_aggregates` | 판정 불가 대응안의 NULL 집계 |
| `test_v5_codes_are_rejected` | `EAST_FULFILL`·`PARTIAL` 거부 |
| `test_stale_reason_codes_exclude_multiple` | 대표 사유는 `MULTIPLE_RELEVANT_CHANGES` 허용, 배열은 거부 |
| `test_on_hold_run_has_failure_summary_and_empty_results` | `ON_HOLD`: 결과 없음, 실패 요약, 기준일 null |
| `test_in_progress_stale_run_has_no_results` | 진행 중에 STALE이 된 실행: 완료 상태·결과·설명·기준일 없음, STALE 사유만 있음 |
| `test_average_daily_sales_is_json_number` | 평균 판매량이 서버 안에서는 `Decimal`, JSON에서는 숫자 |

## 10. 관련 파일

| 파일 | 역할 |
| --- | --- |
| `app/modules/delay_analysis/infrastructure/analysis_enums.py` | 코드값 열거형 |
| `app/modules/delay_analysis/presentation/analysis_run_schemas.py` | 응답 모델 |
| `app/core/schemas.py` | `CamelModel` 공통 베이스 (A 영역, 이번에 수정하지 않음) |
| `tests/modules/delay_analysis/test_analysis_run_schemas.py` | 단위 테스트 |
