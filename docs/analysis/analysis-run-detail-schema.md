# 분석 실행 상세 응답 스키마

`GET /api/v1/analysis-runs/{runId}`(API 명세 v6.2 4.5절)의 응답 모양을 Pydantic 모델로 정의한 내용과, **왜 이렇게 만들었는지**를 설명합니다.
이번 작업 범위는 응답 스키마와 코드값 열거형까지입니다. 라우터·서비스·DB 조회는 아직 없습니다(6절). 서버의 분석 테이블이 아직 ERD v5 구조라, v6.2 전환을 팀이 정한 뒤에 연결합니다.

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
| `tests/modules/delay_analysis/test_analysis_run_schemas.py` | 단위 테스트 15개 (매개변수 포함 실행 수) |

## 3. 왜 이렇게 만들었나

| 결정 | 내용 | 이유 |
| --- | --- | --- |
| 응답 본체는 `CamelModel` | 응답 필드는 camelCase | 명세 D2, 기존 공통 베이스(`app/core/schemas.py`)를 그대로 써서 다른 API와 표기 방식을 맞춤 |
| 저장 JSON 두 개는 `CamelModel`을 쓰지 않음 | `Explanation`·`SkuResults`는 `StoredJsonModel`(별칭 없음, `extra="forbid"`) | 명세 D20: 데이터 팀 계약이 키를 snake_case로 고정하고 모르는 키를 금지함. 설명은 근거 경로(JSON Pointer, 예: `/run/planned_3pl_inbound_date`)까지 검증하므로 키를 바꾸면 근거 경로와 어긋남 |
| 저장 JSON의 식별자는 `uuid.UUID`가 아니라 소문자·하이픈 문자열(`StoredUuid`) | 패턴 `^[0-9a-f]{8}-…$` | `uuid.UUID`는 대문자·하이픈 없는 표기도 받아들이고 출력할 때 바꿔 씀. "저장된 그대로"(D20)와 계약 0절 식별자 규칙에 어긋남(8절 F4) |
| raw 값은 소수 4자리 문자열(`RawDecimal`) | 패턴 `^[0-9]+\.[0-9]{4}$` | 계약 0절: 정수화 경계 재현 때문에 부동소수 금지 |
| `3pl` 필드는 별칭을 직접 지정 | `planned3plInboundDate`, `expected3plInboundDate`, `baselinePlanned3plInboundDate` | `to_camel`이 `planned3PlInboundDate`(P 대문자)로 바꿈. 명세 표기와 다름(8절 F1) |
| 코드값은 `StrEnum` | DDL CHECK 값과 1:1 | 같은 모듈의 `state.py`(`AnalysisStep`)와 같은 방식. 옛 v5 값(`EAST_FULFILL`, `PARTIAL`)이 들어오면 거부됨 |
| STALE 사유를 열거형 두 개로 나눔 | `StaleReason`(대표 사유, ck28), `StaleReasonCode`(배열 항목, ck30) | `MULTIPLE_RELEVANT_CHANGES`는 대표 사유에만 쓰고 배열에는 넣지 않음(DDL ck30·ck31)(8절 F4) |
| 대응안 주문 집계는 `int \| None` | `fullyRecoveredOrders` 등 4개 | 집계 NULL 규칙 G-1~G-3: 전체 판정 불가·사전 조건 실패면 NULL. 0과 "계산 안 함"을 구분해야 함 |
| SKU별 결과의 원값·수량은 모두 null 허용 | `raw_*`, `*_quantity` | 계약은 "EVALUATED면 not null"만 정함. 실제 데이터에는 NOT_ESTIMABLE 항목에도 원값이 채워져 있음(3건). 둘 다 읽을 수 있어야 함 |
| `impact_source`만 키가 없어도 됨 | 기본값 `None` | 계약 v3 0.1절: v1·v2 엔진 산출물에는 키가 없고 "기록 없음"으로 읽음. 다른 키는 "키 생략 금지"(계약 0절) |
| 위험 주문을 주문으로 묶음 | `AtRiskOrder.lines[]` | 명세 D21: `at_risk_orders`는 주문 상세 1행. 화면 "위험 주문 N건"은 주문 수 |
| 기준일은 `date \| None` | `inputBasis.baseDate` | `analysis_runs`에 열이 없고 `dependency_scope.base_date`에만 있음. `ON_HOLD`·`FAILED`는 `dependency_scope`가 NULL(DDL ck36) |
| `runCode`는 `str \| None` | | 명세 D14: v6.2에도 `run_code` 열이 없음. 열이 생기기 전까지 null |
| 응답에서 뺀 것 | `dependency_scope`, `decision_signature`·`decision_signature_sha256`, `raw_*_shortage_quantity` | 명세 4.5 "응답에 넣지 않는 것": 서버 내부 STALE 판정·검증용 |
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
| `STALE` | 완료 당시 값 | 완료 당시 값 | null | 있음 |

## 6. 아직 없는 것

| 항목 | 상태 |
| --- | --- |
| 라우터·서비스·DB 조회 | 없음. 서버 분석 테이블이 v5 구조(Alembic `0001`). v6.2 전환 방식(Alembic/DDL)·담당 결정 뒤에 구현 |
| ORM 행 → 응답 변환 | 없음. 위험 주문을 주문으로 묶는 것, `dependency_scope.base_date` 읽기, route·대표 상품(D13, B의 계산 함수)은 조회 구현에서 함 |
| `runCode` | null. `run_code` 열 추가(D14) 뒤에 채움 |
| 공통 camelCase 변환의 `3pl` 표기 | 이 스키마만 별칭으로 고침. 같은 필드를 쓰는 다른 API(지연 목록·선적)도 같은 문제가 있어 공통 베이스 담당과 공유 필요 |

## 7. 검증

| 검사 | 방법 | 결과 |
| --- | --- | --- |
| 명세 4.5 필드 | 명세 표의 백틱 필드 111개 + 한글 항목 대응을 모델 출력 키(별칭)와 묶음별 비교 | 빠진 필드 없음 |
| 코드값 | 열거형 24종을 DDL CHECK·설명 스키마 enum·계약 v3 값과 집합 비교, 같은 값을 쓰는 다른 자리 3곳 | 일치 (처음엔 2건 실패, 8절 F4) |
| NULL 허용 | DDL 열과 이름이 같은 필드 99개: NOT NULL ⇔ 모델 필수 | 일치 |
| explanation.v1 | 필수 키, 모든 패턴·길이·개수 제한, 모르는 키 금지 | 일치 (처음엔 1건 실패, 8절 F4) |
| sku_results.v1 | 항목 키 16개, donor 키 13개, 최상위 4개 | 일치 |
| 실데이터 | overlay의 설명 13, 실행 코드값 21, 대응안 39, 주문 결과 21, 재고 영향 391, 초안 2, 최종 선택 2 | 전부 읽힘 |
| 검사가 틀린 것을 잡는지 | 옛 `EAST_FULFILL` 추가, NOT NULL 열을 null 허용으로, null 열을 필수로, 설명 길이 600→900, 명세 필드 하나 삭제 | 5건 모두 잡음 |
| 테스트가 틀린 것을 잡는지 | `3pl` 별칭을 지우고 실행 | 1건 실패 확인 후 복원(해시 같음) |
| 린트·테스트 | `uv run ruff check . && uv run ruff format --check . && uv run pytest` | 통과, 전체 53건 |

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

## 9. 테스트

`tests/modules/delay_analysis/test_analysis_run_schemas.py` (DB 없이 실행)

| 테스트 | 확인 내용 |
| --- | --- |
| `test_top_level_fields_follow_spec_camel_case` | 최상위 키가 명세 4.5 필드 + 묶음 5개와 정확히 같음 |
| `test_3pl_dates_use_spec_spelling` | `3pl` 필드가 명세 표기, `3Pl` 없음 |
| `test_stored_json_keeps_snake_case_keys` | 설명·SKU별 결과 안의 키가 저장된 그대로 |
| `test_response_does_not_expose_internal_fields` | 판정 서명·의존 범위·raw 원값이 응답에 없음 |
| `test_explanation_rejects_unknown_key` | 설명에 `delay_cause` 같은 모르는 키 거부 |
| `test_stored_json_ids_must_be_lowercase_hyphenated` | 대문자·하이픈 없는 UUID 거부 |
| `test_sku_results_reads_output_without_impact_source` | 옛 엔진 산출물(키 없음) 읽힘 |
| `test_sku_results_rejects_non_contract_raw_value` | 부동소수·자릿수 다른 raw 값 거부 |
| `test_needs_check_option_allows_null_aggregates` | 판정 불가 대응안의 NULL 집계 |
| `test_v5_codes_are_rejected` | `EAST_FULFILL`·`PARTIAL` 거부 |
| `test_stale_reason_codes_exclude_multiple` | 대표 사유는 `MULTIPLE_RELEVANT_CHANGES` 허용, 배열은 거부 |
| `test_on_hold_run_has_failure_summary_and_empty_results` | `ON_HOLD`: 결과 없음, 실패 요약, 기준일 null |

## 10. 관련 파일

| 파일 | 역할 |
| --- | --- |
| `app/modules/delay_analysis/infrastructure/analysis_enums.py` | 코드값 열거형 |
| `app/modules/delay_analysis/presentation/analysis_run_schemas.py` | 응답 모델 |
| `app/core/schemas.py` | `CamelModel` 공통 베이스 (A 영역, 이번에 수정하지 않음) |
| `tests/modules/delay_analysis/test_analysis_run_schemas.py` | 단위 테스트 |
