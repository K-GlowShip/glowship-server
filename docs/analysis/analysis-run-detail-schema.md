# 분석 실행 상세 응답 스키마

`GET /api/v1/analysis-runs/{runId}`(API 명세 v6.2 4.5절)의 응답 모양을 Pydantic 모델로 정의한 내용과, **왜 이렇게 만들었는지**를 설명합니다.
1~10절은 응답 스키마와 코드값 열거형을 만든 작업(GLOW-93, PR #8·#16)의 기록입니다. 라우터·서비스·DB 조회와 스키마 후속(GLOW-115)은 **11절**에 있고, 1~10절 중 GLOW-115로 바뀐 줄은 그 자리에서 고쳤습니다.

작업 중 실패한 것과 그 원인은 8절(GLOW-93)과 11.5절(GLOW-115)에 빠짐없이 남겼습니다.

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
├── target: AnalysisTarget                대상 지연 (선적·도착 창고·대표 상품 요약 객체, 경로·단계·계획/예상 입고일·지연 일수)
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
├── drafts[]: MemberDraftSummary          작성자 author(사용자 요약)·responseOption(대응안 요약)
└── finalDecision: FinalDecisionSummary   선택자 decidedBy(사용자 요약)·responseOption(대응안 요약)
```

| 파일 | 내용 |
| --- | --- |
| `app/modules/delay_analysis/infrastructure/analysis_enums.py` | 코드값 열거형 25종 (DDL CHECK·설명 스키마·JSON 계약과 같은 값) |
| `app/modules/delay_analysis/presentation/analysis_run_schemas.py` | 응답 모델 |
| `tests/modules/delay_analysis/test_analysis_run_schemas.py` | 단위 테스트 19개 함수, 매개변수 포함 23건 |

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
| `runCode`는 `str \| None` | | 명세 D14: v6.2에도 `run_code` 열이 없음. 열이 생기기 전까지 null. `0006`(PR #21) 뒤로는 열이 NOT NULL이라 항상 값이 있음(2026 H1 21건 확인, 11절) |
| 응답에서 뺀 DB 열 | `analysis_runs.dependency_scope`·`decision_signature`·`decision_signature_sha256`, `inventory_impact_results.raw_*_shortage_quantity` | 명세 4.5 "응답에 넣지 않는 것"(428줄): 서버 내부 STALE 판정·검증용. 단, `explanation.v1` 안의 `decision_signature_hash`·`prompt_template_sha256`은 계약의 필수 메타데이터라 그대로 나감. 명세 4.5 AI 설명 표(482줄)가 이 메타데이터를 "표시하지 않음"(화면에 표시하지 않음)으로 두고, D20이 설명을 저장된 그대로 내리게 함. 판정 서명 본문(`decision_signature`)과 의존 범위는 나가지 않음(8절 F9) |
| 대상의 선적·창고·대표 상품은 요약 객체 | `shipment`(`ShipmentSummary`), `warehouse`(`WarehouseSummary`), `representativeSku`(`SkuSummary \| None`) | 명세 4.5 대상 칸, 응답 본문 규칙 5.2. 지연 목록·상세(4.4)와 같은 모델(`app/core/summary_schemas.py`)과 같은 계산 함수(D13, `delay_display`)를 써서 두 화면의 값과 모양이 같음. 상세가 없는 선적은 대표 상품이 null. 단계·입고일·지연 일수도 지연 목록처럼 선적의 현재 값(8절 F16) |
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
| `AtRiskOrder.orderId` | 주문 묶음의 식별자 |
| ~~`AtRiskOrderLine.skuCode`, `InventoryImpact.skuCode`·`productName`·`warehouseCode`~~ | GLOW-115(PR #32 리뷰)에서 명세 2절 규칙대로 `sku`·`warehouse` 요약 객체로 바꿈(11.2). 옛 설명: 명세는 "SKU·창고별"로만 적음. 화면에 ID만으로는 표시할 수 없음 |
| `DataVersion.dataSyncRunId`·`finishedAt` | "사용한 수집 목록"의 내용 |
| `drafts[].memberDraftId`, `finalDecision.finalDecisionId` | 판단 이력 행의 식별자. 작성자·선택자는 GLOW-115부터 명세 키 `author`·`decidedBy`(사용자 요약 `{userId, displayName, roleCode}`)로 줌(11절) |

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

명세 4.5 "결과 분류와 상태별로 보이는 내용"을 따릅니다. 모델은 아래 경우를 모두 받아들이도록 null·빈 목록을 허용합니다. 조회 서비스(GLOW-115)는 저장된 행을 그대로 읽고 따로 비우거나 만들지 않습니다. 비는 묶음은 저장기가 행을 만들지 않은 결과이고, 실제 DB 21건이 이 표와 맞는지는 시험으로 확인합니다(11절). 표의 `stale*` "null"은 `staleDetectedAt`·`stalePrimaryReason`이고, `staleReasonCodes`는 STALE이 아니면 `[]`입니다(명세 4.5, GLOW-115).

| 상태 | 결과 목록 | `explanation` | 실패 요약 | `stale*` |
| --- | --- | --- | --- | --- |
| `READY_FOR_REVIEW`·`FINALIZED` | 있음 | 있음(검증 통과 시) | null | null |
| `NO_RISK` | 재고 영향만 | null | null | null |
| `NO_DELAY`·`ON_HOLD`·도구 실패 `FAILED` | 빈 목록 | null | `ON_HOLD`·`FAILED`만 있음 | null |
| `FAILED`(`EXPLANATION_GENERATION_FAILED`) | 있음 | null | 있음 | null |
| `STALE`(완료 뒤에 STALE) | 완료 당시 값 | 완료 당시 값 | null | 있음 |
| `STALE`(진행 중에 STALE) | 그때까지 저장된 결과(없으면 빈 목록) | null | null | 있음 |

진행 중(`DETECTED`~`VALIDATING_OUTPUT`)인 실행도 수집 적재 때 STALE이 됩니다. develop `stale_service.py`의 STALE 대상(`STALE_TARGET_STATUSES`)에 진행 중 상태가 들어 있고, 의존 범위 없이 판정합니다. 이미 저장한 계산 결과(예: 재고 영향·위험 주문)는 지우지 않고 그대로 보여 주지만, 설명·추천이 없고 선택 대상이 아닙니다(serviceflow 399줄). 이 실행은 `completedStatus`·`resultClass`·`inputBasis.baseDate`도 null입니다(명세 4.5 "이때 `completedStatus`는 비어 있다", DDL ck25는 STALE이면서 `completed_status`가 없는 경우를 허용)(8절 F17).

## 6. 아직 없는 것

| 항목 | 상태 |
| --- | --- |
| ~~라우터·서비스·DB 조회~~ | GLOW-115에서 만듦(11절) |
| ~~ORM 행 → 응답 변환~~ | GLOW-115에서 만듦(11절) |
| ~~`runCode`~~ | `0006` 뒤로 값이 있음 |
| `dataWarnings[]` 실제 값 | 아직 항상 `[]`. 입력 확정 때의 수집 상태를 남기는 일은 백그라운드 실행·저장 작업에서 함(GLOW-115 Jira "팀원 참고사항") |
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
| 린트·테스트 | `uv run ruff check . && uv run ruff format --check . && uv run pytest` | 통과. 이 문서의 테스트 23건, 전체 371건 (develop `2f651d6` + `0005` + PR #16 리뷰 반영 기준, 건너뛴 테스트 없음. 리뷰 반영 전 21건·368건, 후속 수정을 처음 쓴 `e96db1f` 기준 93건, PR #8 머지 때 92건, 첫 작성 때 53건) |

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
| F16 | (리뷰 #16) 대상 묶음이 `shipmentCode`·대표 상품명 문자열만 주고 도착 창고가 없어, 명세 4.5 대상 칸·지연 목록(4.4)의 요약 객체 모양과 다름 | PR #8에서 대상 묶음을 만들 때는 요약 객체(`summary_schemas.py`)가 없었고, PR #11에서 요약 객체가 생긴 뒤 이 묶음을 다시 맞추지 않음. 같은 값을 두 API가 따로 정의해 한쪽만 바뀜 | 지연 목록과 같은 요약 모델로 바꾸고, 대상 키를 명세와 비교하는 테스트와 두 모델이 같은 클래스인지 보는 테스트를 추가. 고치기 전 코드에서 실패 확인 |
| F17 | (리뷰 #16) 5절이 진행 중에 STALE이 된 실행의 결과를 "빈 목록"으로 적음. serviceflow 399줄은 "이미 저장한 계산 결과는 보존" | F13을 고칠 때 develop 코드(`stale_service.py`, 의존 범위 없음)만 보고 결과도 없다고 추론하고, serviceflow의 동시성 표를 확인하지 않음 | 5절 표·설명, 응답 모델 주석을 "그때까지 저장된 결과"로 고치고, 테스트를 저장된 결과가 남는 경우로 바꿈. 모델은 원래 두 경우를 다 받아들여서, 이 테스트는 모양을 고정하는 기록용이며 고치기 전 코드에서도 통과함 |

## 9. 테스트

`tests/modules/delay_analysis/test_analysis_run_schemas.py` (DB 없이 실행)

| 테스트 | 확인 내용 |
| --- | --- |
| `test_top_level_fields_follow_spec_camel_case` | 최상위 키가 명세 4.5 필드 + 묶음 5개와 정확히 같음 |
| `test_3pl_dates_use_spec_spelling` | `3pl` 필드가 명세 표기, `3Pl` 없음 |
| `test_target_matches_spec_and_delay_list_summaries` | 대상 묶음 키가 명세 4.5 대상 칸과 같고, 선적·창고·대표 상품이 지연 목록과 같은 요약 모델 |
| `test_target_without_shipment_lines_has_null_representative_sku` | 상세가 없는 선적은 `representativeSku: null` |
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
| `test_in_progress_stale_run_keeps_stored_results` | 진행 중에 STALE이 된 실행: 저장된 재고 영향·위험 주문은 남고, 대응안·설명·완료 상태·기준일은 없음 |
| `test_average_daily_sales_is_json_number` | 평균 판매량이 서버 안에서는 `Decimal`, JSON에서는 숫자 |

## 10. 관련 파일

| 파일 | 역할 |
| --- | --- |
| `app/modules/delay_analysis/infrastructure/analysis_enums.py` | 코드값 열거형 |
| `app/modules/delay_analysis/presentation/analysis_run_schemas.py` | 응답 모델 |
| `app/core/schemas.py` | `CamelModel` 공통 베이스 (A 영역, 이번에 수정하지 않음) |
| `tests/modules/delay_analysis/test_analysis_run_schemas.py` | 단위 테스트 |

## 11. 조회 API 구현과 스키마 후속 (GLOW-115)

`GET /api/v1/analysis-runs/{runId}`를 만들었습니다. 저장된 실행·결과 행을 읽어 위 스키마로 돌려줍니다. 근거는 Jira GLOW-115 설명, 명세 v6.2 4.5(`glowship-plan` `dec7685`, README 해시 5개 일치), 응답 본문 규칙 5.2입니다. 기준 브랜치는 develop `8717165`입니다.

### 11.1 만든 것

| 파일 | 내용 |
| --- | --- |
| `presentation/analysis_run_api_router.py` | 라우터. Client(팀원·팀장)만. 없는 실행·다른 고객사 실행은 같은 `404 ANALYSIS_RUN_NOT_FOUND`(명세 6절 `{리소스}_NOT_FOUND`) |
| `application/analysis_run_detail_service.py` | 고객사 범위·RLS 설정, 대상 계산(D13), `dataBasisAt` 계산. 저장된 결과를 그대로 묶어 돌려줌 |
| `infrastructure/repository/analysis_run_detail_repository.py` | 읽기 전용 SQL 8개(실행, 사용한 수집, 재고 영향, 위험 주문 줄, 대응안, 주문별 결과, 초안, 최종 선택). 모두 `company_id` 조건 |
| `application/exception/analysis_run_query_exceptions.py` | `ANALYSIS_RUN_NOT_FOUND` |
| `presentation/analysis_run_schemas.py` | 스키마 후속(11.2)과 저장 행 → 응답 변환(`AnalysisRunDetailResponse.of`) |
| `app/core/summary_schemas.py` | 사용자 요약 `UserSummary {userId, displayName, roleCode}`, 대응안 요약 `ResponseOptionSummary {responseOptionId, optionType}`(응답 본문 규칙 5.2 표) |
| `application/delay_event_query_service.py` | `_summarize`를 `summarize_shipment`로 공개. 지연 조회(4.4)와 이 API의 대상이 같은 함수를 씀(D13) |

### 11.2 스키마 후속 (Jira "응답 스키마 후속")

| 바꾼 것 | 전 | 후 | 근거 |
| --- | --- | --- | --- |
| 대응안 사유 문구 | `reason` 있음 | 뺌 | 명세 4.5 "응답에 넣지 않는 것": 저장기가 `reason_code`와 같은 값을 넣고, 화면 문구는 `reasonCode` 라벨로 만듦 |
| horizon 밖 여부 | `expectedBeyondHorizon` | `isBeyondHorizon` | 명세 4.5 위험 주문 칸 |
| 재시도 소진 | `retryExhausted` | `isRetryExhausted` | 명세 4.5 실패 요약 칸 |
| 공급 창고 | `sourceWarehouseId` | `sourceWarehouse`(창고 요약, 없으면 null) | 명세 4.5 주문별 결과 칸, 규칙 5.2 |
| 초안 작성자·대응안 | `userId`·`displayName`·`responseOptionId` | `author`(사용자 요약)·`responseOption`(대응안 요약, 의견만이면 null) | 명세 4.5 판단 이력 칸, 규칙 5.2 키 이름(사용자는 역할 이름만) |
| 최종 선택자·대응안 | `userId`·`displayName`·`responseOptionId` | `decidedBy`·`responseOption` | 같음 |
| STALE 사유 배열 | STALE이 아니면 null | STALE이 아니면 `[]`(null 거부) | 명세 4.5 STALE 칸 |
| 소수 둘째 자리 | — | 바꿀 필드 없음 | 응답의 소수 필드는 `averageDailySales` 하나이고, 이 필드는 규칙 2.2.1 예외(저장값 6자리, 명세 C29)라 그대로 둠. 원값(`raw_*`)은 응답에 없음 |
| 재고 영향 SKU·창고 (PR #32 리뷰) | `skuId`·`skuCode`·`productName`·`warehouseId`·`warehouseCode` | `sku`(`skuId`, `skuCode`, `productName`)·`warehouse`(`warehouseId`, `warehouseCode`, `regionCode`) | 명세 2절 "연관 대상"(113행): 한글 항목 "SKU"·"창고"는 요약 객체. 응답 본문 규칙 5번 |
| 위험 주문 줄 SKU (PR #32 리뷰) | `skuId`·`skuCode` | `sku`(SKU 요약) | 같음(명세 4.5 위험 주문 `lines[]`의 "SKU") |
| 재분석 요청자 (PR #32 리뷰) | `requestedBy`(사용자 ID) | `requestedBy`(사용자 요약, 자동 실행이면 null) | 응답 본문 규칙 5번, 2.2 이름 표(사용자는 역할 이름만). 2026 H1 실행 2건에 요청자가 있음 |
| 저장 JSON을 읽지 못할 때 (PR #32 리뷰 [should]) | 엄격 모델 검증에 실패하면 상세 전체 500 | `explanation`·`skuResults`만 null, 서버 오류 로그(열 이름·행 ID·오류 수) | 계약이 바뀌거나(예: `explanation.v2`) 키가 하나 늘어도 한 실행 때문에 화면 전체가 깨지지 않게 함. 저장된 `explanationValidationStatus`는 그대로라 `PASSED`인데 `explanation`이 null일 수 있음. 응답 모양(OpenAPI)은 계약 모델 그대로 둠 |

**처음 결정과 바뀐 점:** 11-05 처음에는 재고 영향·위험 주문 줄의 SKU·창고를 낱개 필드로 두었습니다. 이때 명세 4.5 표만 보고 2절 공통 규칙(113행, 11-03부터 있음)을 확인하지 않았습니다(11.5 G6). PR #32 리뷰(KatherineJH)의 지적으로 요약 객체로 고쳤습니다.

### 11.3 왜 이렇게 만들었나

| 결정 | 이유 |
| --- | --- |
| 저장된 행을 그대로 읽고 상태별로 따로 비우지 않음 | Jira: "이 작업은 저장된 결과 읽기". 비는 묶음은 저장기가 행을 만들지 않은 결과임. 서비스가 상태를 보고 지우면 저장값과 응답이 어긋날 수 있음. 대신 실제 DB 21건이 명세 상태 표와 맞는지 시험으로 확인(11.4) |
| 대상은 지연 조회 저장소(`find_delay_event`·`find_shipment_lines`)와 `summarize_shipment` 재사용 | 명세 4.5 "4.4 지연 목록과 같은 계산 함수(D13)". 따로 쓰면 두 화면의 경로·대표 상품·지연 일수가 어긋날 수 있음(8절 F16과 같은 원인을 피함) |
| 위험 주문의 주문 단위 값은 그 주문의 첫 줄(가장 작은 줄 번호)에서 읽음 | `at_risk_orders`는 줄마다 귀속·약속·예상 납품·사유를 가짐. 2026 H1 주문 3건(모두 여러 줄)은 줄끼리 값이 같음(11-05 SQL 확인). DDL이 보장하지는 않음(7절 한계와 같음). 줄끼리 다르면 첫 줄 값을 쓰고 경고 로그(실행 ID·주문 ID·줄)를 남김(PR #32 리뷰) |
| 대응안 순서는 고정 순서(KEEP → 출항 전 항공 → 한국 창고 항공 → 다른 권역) | 설명의 `option_explanations[]`와 같은 순서(명세 4.5 AI 설명 표). 추천 순위는 `recommendationRank`로 따로 있음 |
| `dataBasisAt` = 사용한 수집 중 `finished_at`이 가장 이른 것, 끝나지 않은 수집은 뺌 | 명세 4.5 입력 기준 칸 "사용한 수집 중 가장 오래된 수집 시각" |
| `dataWarnings`는 `[]` | Jira "팀원 참고사항": 실제 값은 백그라운드 실행·저장 작업 뒤 |
| `sku_results` 모델(`SkuResultItem` 등)은 건드리지 않음 | PR #26(GLOW-112)이 이 모델을 `infrastructure/stored_json_schemas.py`로 옮김. 겹침을 줄이려고 그 구역은 그대로 두고, #26 머지 뒤 develop을 병합하기로 함(사용자 결정 11-05) |

**알려 둘 점:** 저장된 `sku_results` 항목에 `impact_source` 키가 없으면(v1·v2 엔진 산출물) 응답에는 `"impact_source": null`이 붙습니다(`SkuResultItem` 기본값 `None`). D20 "저장된 그대로"와 엄밀히는 다릅니다. 2026 H1 저장값 856항목은 모두 키가 있어 실제 응답은 저장값과 같습니다(11.4 실제 DB 시험). 이 모델은 #26(머지됨)이 만든 공용 파일 `infrastructure/stored_json_schemas.py`에 있고 에이전트 도구 모델도 함께 쓰므로, 이 PR에서는 고치지 않고 후속으로 다룹니다(PR #32 리뷰 답, 사용자 결정 11-05).

### 11.4 검증

| 검사 | 방법 | 결과 |
| --- | --- | --- |
| 저장값 일치(인수 조건) | `test_analysis_run_detail_sql.py`: DB 실행 21건을 모두 응답으로 만들고, 서비스와 따로 쓴 SQL로 읽은 값과 비교(실행·STALE·실패 요약 16필드, 대상, 입력 기준, 재고 영향, 위험 주문 줄, 대응안·`skuResults`·주문별 결과, 초안·최종 선택, 설명 JSON 전체) | 통과 |
| 상태 표 | 같은 파일: `NO_DELAY`·`ON_HOLD`·도구 실패 `FAILED`는 결과 묶음이 비고, 완료 판정이 `READY_FOR_REVIEW`·`NO_RISK`면 재고 영향·기준일이 있음, STALE 사유, 설명 실패 | 통과. 21건 = STALE 14·FINALIZED 2·FAILED 2(도구 실패 1·설명 실패 1)·NO_DELAY 1·NO_RISK 1·ON_HOLD 1 |
| DB에 없는 상태 | `test_analysis_run_api.py`: `READY_FOR_REVIEW`(STALE 아님), 진행 중(`ANALYZING_IMPACT`), 여러 줄 주문 묶기, 대응안별 결과 붙이기 | 통과 |
| 권한 | 같은 파일: 팀원·팀장 200, ADMIN·OWNER 403, 쿠키 없음 401, 없는 실행 404, UUID 아님 422 | 통과 |
| 다른 고객사 | 실제 DB: 21건마다 다른 고객사로 서비스 404, 저장소 조회 8개가 모두 빈 결과 | 통과 |
| 실제 서버 | 8003 서버에 21건 GET(시험용 토큰, 조회만) | 21건 모두 200, 다른 고객사 404 `ANALYSIS_RUN_NOT_FOUND`, ADMIN 403 |
| 시험이 틀린 것을 잡는지(변이 시험) | 코드 한 곳을 망가뜨리고 실제 DB 시험 실행 후 원본 복구(바이트 비교): M1 실행 조회 고객사 조건 제거, M2 주문 줄 첫 줄만, M3 STALE 사유 기본값을 다른 값으로, M4 공급 창고 조인 끊기, M5 부족 수량에 기준선 값, M6 위험 주문 조회 고객사 조건 제거 | 처음엔 M1·M6을 못 잡음(11.5 G1·G2). 시험을 고친 뒤 6건 모두 잡음. PR #32 리뷰 반영 때 M7 요청자 조인 열 틀림, M8 창고 지역 자리에 창고 코드를 더해 8건 모두 잡음 |
| 린트·전체 시험 | `uv run --locked ruff check .`·`ruff format --check .`, `uvx --python 3.12 ruff@latest check .`·`format --check .`, `uv run --locked pytest` | 통과. 전체 727건(develop `8717165` 703건 + 새 24건), 건너뜀 0. develop `cbc406a`(#25·#26·#27) 병합 뒤(DB 0007): 816건 통과, 건너뜀 6(엔진 로더 시험, `.env`에 `GLOWSHIP_ENGINE_DIR` 없음). 변이 6건·실제 서버 21건도 병합 뒤 다시 확인 |

**검증의 한계**
- 실제 DB에 `READY_FOR_REVIEW`(STALE 아님)·진행 중 실행·`READY_ORDER_RISK`·`DELAY_INDUCED` 위험 주문이 없어 이 경우는 직접 만든 행으로만 확인했습니다.
- 위험 주문은 2026 H1 전체에 주문 3건(한 실행)뿐입니다.

### 11.5 작업 중 실패와 원인

| # | 실패 | 원인 | 조치 |
| --- | --- | --- | --- |
| G1 | 실제 DB 시험이 처음부터 모두 통과했는데, 실행 조회의 고객사 조건을 지워도(M1) 통과함 | 다른 고객사 404를 서비스 결과로만 봄. 실행 조회가 다른 고객사 실행을 돌려줘도 뒤이은 지연 조회(고객사 조건 있음)가 None이라 결국 404가 남 | 저장소 조회 각각을 다른 고객사로 불러 비는지 따로 확인 |
| G2 | G1을 고친 뒤에도 위험 주문 조회의 고객사 조건을 지운 변이(M6)를 못 잡음 | "결과 행이 다 있는 실행 하나"를 골랐는데, 그 조건(최종 선택 + 주문별 결과)에 맞는 실행이 DB에 0건이라 위험 주문이 없는 실행으로 시험함 | 실행 21건 모두를 돌고, 각 조회가 자기 고객사로는 행을 돌려주는 실행이 하나 이상 있는지도 확인 |
| G3 | 새 단위 시험 1건 실패: 응답 `skuResults`에 `impact_source: null`이 붙음 | 시험 입력에 `impact_source` 키를 넣지 않음. 모델 기본값이 null을 붙이는 동작은 원래 있던 것(11.3 "알려 둘 점") | 실제 데이터 856항목에 키가 모두 있음을 확인하고 시험 입력을 실제 데이터처럼 고침. 동작은 #26 구역이라 보고만 함 |
| G4 | 8003 서버가 `summary_schemas.py` 변경 뒤 "Reloading..."에서 멈추고, 옛 프로세스가 새 경로 없이 계속 응답함 | Windows에서 `uvicorn --reload`의 재시작이 끝나지 않음 | 8003만 내리고 `--reload` 없이 다시 띄움. OpenAPI에 새 경로가 있는지 확인한 뒤 실제 서버 검사 |
| G5 | 첫 `ruff check`에서 import 순서 2건 | 스키마 파일에 저장소 import를 스크립트로 넣으면서 정렬하지 않음 | `ruff check --fix` 뒤 고정 버전·최신(3.12) 둘 다 통과 확인 |
| G6 | (리뷰 #32, 변경 요청) 재고 영향·위험 주문 줄의 SKU·창고를 낱개 필드로 둠 | 계획 때 명세 4.5 표와 머지된 스키마만 보고, 2절 "연관 대상" 공통 규칙(113행)을 확인하지 않은 채 "낱개 유지"를 추천함 | `sku`·`warehouse` 요약 객체로 고침. 실제 DB 대조 시험이 요약 객체 안의 코드·이름·지역까지 SQL과 비교하게 함 |
| G7 | (리뷰 #32) `requestedBy`만 사용자 ID로 남음 | 판단 이력의 `author`·`decidedBy`는 요약으로 바꾸면서 같은 응답의 요청자는 보지 않음 | 사용자 요약으로 고침(`users` LEFT JOIN). 요청자 있는 실행 2건을 실제 DB 시험이 대조 |

### 11.6 시험 (GLOW-115에서 더한 것)

| 파일 | 시험 | 확인 내용 |
| --- | --- | --- |
| `test_analysis_run_schemas.py` | `test_option_has_no_reason_field`, `test_flags_use_is_prefix`, `test_stale_reason_codes_is_empty_list_when_not_stale`, `test_outcome_source_warehouse_is_warehouse_summary`, `test_decision_history_uses_user_and_option_summaries` | 11.2 후속 필드 모양. 기존 시험 입력도 새 필드 이름으로 고침 |
| `test_analysis_run_api.py`(DB 없음) | 응답 변환 8건, 라우터 5개 함수(매개변수 포함 7건, 401·403은 오류 코드 이름까지), PR #32 리뷰 반영 7건(SKU·창고 요약 2, 요청자 1, 저장 JSON 읽기 실패 2, 주문 단위 값 불일치 경고 2) | 11.4 "DB에 없는 상태"·"권한", 11.2 리뷰 반영 줄 |
| `test_analysis_run_detail_sql.py`(실제 DB, 읽기 전용·롤백) | `test_every_run_matches_stored_values`, `test_result_rows_match_stored_rows`, `test_status_table_empty_bundles`, `test_other_company_and_missing_run_are_same_404` | 11.4 "저장값 일치"·"상태 표"·"다른 고객사". DB에 접속할 수 없거나 head가 아니면 건너뜀(`db_session`) |
