# 상품 목록 조회

고객사 사용자가 "연동 데이터 조회" 화면의 **상품 탭**에서 보는 상품(SKU) 목록 API가 **현재 어떻게 동작하는지** 설명합니다.
응답 모양은 API 명세 v6.2 4.3 `GET /skus`와 응답 본문 규칙 6절(목록)을 따릅니다.
상단 카드와 탭 배지(원천별 `openIssueCount`)는 [동기화 상태 API](../data-sync/latest-sync-status.md), 하단 오류 배너는 `GET /validation-issues`(미구현) 몫입니다.

> 2026-11-03 프론트 요청 반영: 행에 `hs10`(상세 모달)을 추가했고, `externalCodes[]`는 SKU 코드 원천(INVENTORY·SHIPMENT)마다 항목을 항상 내려주며 매핑이 없으면 `externalCode = null`·`mappingStatus = UNMAPPED`입니다. 명세 4.3의 `CODE_MISSING` 문구("MAPPED가 아닌 매핑이 있으면")는 DDL 제약과 맞지 않아 "매핑이 없는 원천이 있으면"으로 바꿔 구현했습니다(3절).

## 1. 전체 흐름

```mermaid
sequenceDiagram
    participant W as glowship-web
    participant S as glowship-server
    participant DB as PostgreSQL

    W->>S: GET /api/v1/skus?q=&onlyErrors=&page=&size= (access_token 쿠키)
    S->>S: 쿼리 파라미터 검증 (실패 시 422)
    S->>S: get_tenant_context: 쿠키 검증, 토큰의 company_id 확인 (없으면 403)
    S->>DB: set_config('app.user_id', 사용자) · set_config('app.company_id', 고객사)
    S->>DB: skus + 외부 코드 집계 + OPEN 이슈 여부 한 페이지 (SQL 1회)
    S->>DB: 같은 필터의 건수 (SQL 1회)
    S->>S: validationStatus 판정
    S-->>W: 200 {items, page, size, totalCount, totalPages}
```

- 고객사 범위는 **토큰의 `company_id`** 입니다. OWNER·ADMIN은 소속 고객사가 없어 403입니다.
- `skus`·`external_entity_mappings`·`data_validation_issues`는 고객사 범위 테이블이라 RLS 컨텍스트를 먼저 설정합니다.

## 2. 조회 API

`GET /api/v1/skus` — `app/modules/master_data/presentation/sku_api_router.py`

### 권한

`access_token` 쿠키가 유효하고 토큰에 `company_id`가 있어야 합니다(`CLIENT_MEMBER`·`CLIENT_LEAD`).

### 쿼리 파라미터

| 이름 | 타입 | 기본값 | 설명 |
| --- | --- | --- | --- |
| `q` | 문자열, 최대 100자 | 없음 | 상품명 또는 ERP 코드 부분 일치. 대소문자 무시. `%`·`_`는 글자 그대로 찾음. 양끝 공백은 지우고, 비면 없는 것으로 봄 |
| `onlyErrors` | 불리언 | `false` | `validationStatus`가 `OK`가 아닌 상품만 |
| `page` | 정수 ≥ 1 | `1` | 페이지 번호 (`app/core/pagination.py` `PageParams`) |
| `size` | 정수 1~100 | `20` | 페이지 크기 |

정렬은 `skuCode` 오름차순, 같으면 `skuId`입니다(명세 4.3).

### 성공 응답

```http
HTTP/1.1 200 OK
```

```json
{
  "items": [
    {
      "skuId": "0199a1b2-…",
      "skuCode": "SKU-201",
      "productName": "B Bloom Hydra Toner Pad 60ea",
      "brandName": "B Bloom",
      "hs10": "3304995000",
      "category": "SKINCARE",
      "subcategory": "Toner Pad",
      "popularityTier": "HIT",
      "isEmergencyAirEligible": true,
      "unitCode": "EA",
      "unitsPerCase": 24,
      "status": "ACTIVE",
      "externalCodes": [
        { "sourceDomain": "INVENTORY", "externalCode": "WMS-SKU-201", "mappingStatus": "MAPPED" },
        { "sourceDomain": "SHIPMENT", "externalCode": "TMS-SKU-201", "mappingStatus": "MAPPED" }
      ],
      "validationStatus": "ISSUE_OPEN"
    },
    {
      "skuId": "0199a1b3-…",
      "skuCode": "SKU-202",
      "productName": "B Bloom Hydra Serum 30ml",
      "brandName": "B Bloom",
      "hs10": "3304995000",
      "category": "SKINCARE",
      "subcategory": "Serum",
      "popularityTier": "HIT",
      "isEmergencyAirEligible": true,
      "unitCode": "EA",
      "unitsPerCase": 48,
      "status": "ACTIVE",
      "externalCodes": [
        { "sourceDomain": "INVENTORY", "externalCode": "WMS-SKU-202", "mappingStatus": "MAPPED" },
        { "sourceDomain": "SHIPMENT", "externalCode": null, "mappingStatus": "UNMAPPED" }
      ],
      "validationStatus": "CODE_MISSING"
    }
  ],
  "page": 1,
  "size": 20,
  "totalCount": 20,
  "totalPages": 1
}
```

| 필드 | 출처 | 설명 |
| --- | --- | --- |
| `skuId` … `status` | `skus` | `skuCode`가 화면의 "ERP 코드". ERP가 상품 마스터의 원천이라 매핑 없이 그대로 씀 |
| `hs10` | `skus.hs10` | HS 10자리 코드(문자열, NOT NULL). 상품 상세 모달에 표시 |
| `popularityTier` | `skus.popularity_tier` | `HIT`·`REGULAR`·`LONG_TAIL`. 화면의 "히트 상품"은 `HIT` |
| `status` | `skus.status` | `ACTIVE`·`INACTIVE`. INACTIVE도 목록에 포함 |
| `externalCodes[]` | `external_entity_mappings` (`entity_type = 'SKU'`, `mapping_status = 'MAPPED'`) | SKU 코드를 쓰는 원천(`INVENTORY` = WMS, `SHIPMENT` = TMS) 순으로 **항목이 항상 하나씩**. 매핑이 있으면 그 코드와 `MAPPED`, 없으면 `externalCode = null`·`mappingStatus = UNMAPPED`. `[]`는 나오지 않음. 창고별 SKU 코드는 제공하지 않음(명세 D15·U8) |
| `validationStatus` | 서버 판정 | `OK`·`CODE_MISSING`·`ISSUE_OPEN`. 규칙은 3절 |
| `page`·`size`·`totalCount`·`totalPages` | `Page[T]` | `totalCount`는 `q`·`onlyErrors`를 적용한 전체 건수. 범위 밖 페이지는 `items: []`에 실제 `totalCount` |

### 프론트의 해석

| 화면 | 값 |
| --- | --- |
| 상품 / ERP 코드 / 히트 상품 | `productName` / `skuCode` / `popularityTier == "HIT"` |
| 원천별 외부 코드 열 (목업의 "LA / NJ 창고 코드" 열을 대체, 명세 U8) | `externalCodes[]`. 코드가 누락된 원천은 `externalCode == null`(또는 `mappingStatus != "MAPPED"`) |
| 상세 모달의 HS 코드 | `hs10` (별도 상세 API 없음, 목록 행의 값을 씀) |
| 검증 상태 배지 | `validationStatus`. 표시명은 화면이 정함(서버는 코드값만) |
| 오류만 보기 | `onlyErrors=true` |
| 탭 옆 오류 건수 | 이 API가 아니라 `GET /data-sync-runs/latest`의 `PRODUCT` 항목 `openIssueCount` |
| 하단 배너의 메시지·영향 | `GET /validation-issues`(미구현) |

### 실패 응답

| 상태 | code | 상황 |
| --- | --- | --- |
| 401 | `UNAUTHENTICATED` | 쿠키 없음·만료·위조 |
| 403 | `FORBIDDEN` | 토큰에 `company_id`가 없음 (OWNER·ADMIN) |
| 422 | `VALIDATION_ERROR` | `page < 1`, `size`가 1~100 밖, `q`가 100자 초과, `onlyErrors`가 불리언이 아님 |
| 500 | `INTERNAL_SERVER_ERROR` | DB 장애 등 |

명세 5절의 400(D3)·`UNAUTHORIZED`·`ROLE_NOT_ALLOWED`·`requestId`는 core 공통 변경이라 별도 작업입니다.

### 상품 상세

`GET /api/v1/skus/{skuId}` — 상품 탭의 상세 모달에서 씁니다(GLOW-136). 명세에 없던 API라서 설계 담당에게 4.3절 반영을 요청합니다.

- 권한과 고객사 범위는 목록과 같습니다. 없는 상품과 다른 고객사 상품은 같은 404(`SKU_NOT_FOUND`)이고, `skuId`가 UUID가 아니면 422입니다.
- 응답은 목록 행과 같은 필드에 아래 두 배열을 더합니다. 해당 항목이 없으면 빈 배열입니다.

| 필드 | 조건 | 정렬 |
| --- | --- | --- |
| `issues[]` (`dataValidationIssueId`, `sourceRecordKey`, `fieldName`, `errorCode`, `errorMessage`, `severity`, `detectedAt`) | `ISSUE_OPEN`과 같습니다: `OPEN`, `INVENTORY`, 키 둘째 토막 = `skuCode` | `detectedAt` 최신순, 같으면 이슈 ID |
| `affectedDelayEvents[]` (`delayEventId`, `shipment`, `currentRun`) | 이 SKU가 `shipment_lines`에 있는 선적의 **열린 지연**(`closed_at IS NULL`) 중 **최신 실행**(`is_latest`)이 `ON_HOLD`인 것. `currentRun`은 공통 `RunSummary`입니다 | `detectedAt` 최신순, 같으면 지연 ID |

- **이슈를 거치지 않는 이유:** 매핑이 없으면 이슈 키의 SKU 자리에 외부 코드가 들어가 내부 상품을 찾을 수 없고, `CODE_MISSING`은 이슈가 없는 상품에도 나옵니다. 그래서 영향 지연은 선적 상세로 찾습니다.
- **`affectedDelayEvents`의 뜻:** "이 상품이 포함되고 보류 중인 지연"입니다. 다른 사유로 보류된 실행도 나오므로 "이 상품 때문에 보류됐다"는 뜻으로 쓰면 안 됩니다.

## 3. `validationStatus` 판정 규칙

`SkuListService._validation_status` — `app/modules/master_data/application/sku_list_service.py`

| 순서 | 상태 | 조건 |
| --- | --- | --- |
| 1 | `CODE_MISSING` | SKU 코드 원천(`INVENTORY`·`SHIPMENT`) 중 이 SKU의 `MAPPED` 매핑이 없는 원천이 있음 (= `externalCodes[]`에 `externalCode = null` 항목이 있음) |
| 2 | `ISSUE_OPEN` | 이 SKU 코드로 열린 검증 오류가 있음 (아래) |
| 3 | `OK` | 그 밖 |

둘 다 해당하면 `CODE_MISSING`입니다(명세의 나열 순서).

### 매핑 누락을 "MAPPED 행의 부재"로 판정하는 이유

명세 4.3은 "원천 중 `MAPPED`가 아닌 매핑이 있으면 `CODE_MISSING`"이라고 적었지만, DDL 제약 `external_entity_mappings_ck5`는 `MAPPED`가 아닌 행의 `sku_id`를 NULL로 강제합니다.
즉 `PENDING`·`UNMAPPED` 행은 "들어왔지만 어느 SKU에도 붙지 않은 외부 코드"라 특정 SKU에 연결할 수 없고, 그 문구대로 구현하면 `CODE_MISSING`이 절대 나오지 않습니다.
그래서 SKU 코드를 쓰는 원천 목록을 서버 상수(`SKU_CODE_SOURCE_DOMAINS`, `app/modules/master_data/infrastructure/sku_code_sources.py`)로 두고, 원천마다 `MAPPED` 행이 있는지로 판정합니다.

| 원천 | 포함 여부 | 이유 |
| --- | --- | --- |
| `INVENTORY` (WMS) | 포함 | 재고 행이 WMS SKU 코드로 들어옴 |
| `SHIPMENT` (TMS) | 포함 | 선적 상세가 TMS SKU 코드로 들어옴 |
| `PRODUCT` (ERP) | 제외 | 상품 마스터의 원천. `skuCode` 자체가 ERP 코드 |
| `ORDER` (ERP) | 제외 | 주문 상세가 ERP 코드(`sellerSku`)를 그대로 씀 |
| `WAREHOUSE` | 제외 | 창고 엔티티용 |

이 API의 `mappingStatus`는 그래서 `MAPPED`·`UNMAPPED` 두 값만 옵니다. DB의 고아 외부 코드(`PENDING`·`UNMAPPED` 행) 목록은 상품 탭 범위가 아니며, 필요하면 별도 API로 정합니다.
2026 H1 합성데이터는 SKU 50개 모두 두 원천 매핑이 있어 `CODE_MISSING`이 나오지 않습니다. 화면 확인은 로컬에서 매핑 행을 지운 상태로 합니다.

### 이슈 ↔ 상품 연결

`data_validation_issues`에는 상품을 가리키는 `sku_id`가 없습니다. 상품 코드는 `source_record_key` 문자열 안에만 있습니다.
그래서 당분간 다음 규칙으로 잇습니다.

| 조건 | 값 |
| --- | --- |
| `status` | `OPEN` |
| `source_entity_type` | `INVENTORY` |
| 상품 코드 | `split_part(source_record_key, '\|', 2)` — 키 형식 `창고코드\|상품코드\|날짜`의 둘째 토막 |

ORDER·SHIPMENT 이슈는 주문·선적 탭의 몫이라 상품에 붙이지 않습니다. 주문 이슈의 키는 주문 번호라 상품 코드가 들어 있지도 않습니다.

> 키 파싱은 합성데이터의 형식에 의존하는 임시 방식입니다. 수집기(GLOW-13)가 이슈를 기록할 때 `sku_id`·`warehouse_id`를
> 채우도록 컬럼을 추가하는 것이 정식 방법이며, 그때 이 파싱을 조인으로 바꿉니다. SQL의 해당 줄에 주석을 남겨 두었습니다.

## 4. SQL

`SkuRepository` — `app/modules/master_data/infrastructure/repository/sku_repository.py`

| CTE | 내용 | 쓰는 값 |
| --- | --- | --- |
| `sku_sources` | SKU 코드 원천 상수를 `VALUES (순서, 원천)`으로 펼침 | 항목 순서 |
| `external_codes` | `skus × sku_sources`에 `MAPPED` 매핑을 LEFT JOIN 해 SKU별로 `jsonb_agg` (원천 순). 매핑이 없으면 코드 `null`·`UNMAPPED`, `has_unmapped = BOOL_OR(코드 IS NULL)` | `externalCodes[]`, `has_unmapped` |
| `open_issue_skus` | OPEN·INVENTORY 이슈의 키 둘째 토막 (`DISTINCT`) | `has_open_issue` |
| `filtered` | `skus` ⋈ 위 두 CTE, `q`·`onlyErrors` 필터 | 목록·건수 공통 |

목록은 `ORDER BY sku_code, sku_id OFFSET LIMIT`, 건수는 같은 `filtered`의 `COUNT(*)`입니다. 범위 밖 페이지에서도 `totalCount`가 실제 값이 되도록 두 문장으로 나눕니다.
상세는 같은 `filtered`에서 `sku_id`로 한 건을 읽어 공통 필드와 판정이 목록과 어긋나지 않게 합니다. 이어서 이슈와 영향 지연을 각각 읽어 요청당 SQL은 3회입니다. 영향 지연의 상품 포함 여부는 `EXISTS`로 봅니다. 같은 SKU의 선적 줄이 여러 개여도 지연이 겹치지 않게 하려는 것입니다.

`text()` + `bindparam`으로 쓰고 `company_id` 조건을 명시해 로컬(superuser)에서도 운영(RLS)과 같은 결과가 나오게 합니다.

## 5. 테스트

단위 테스트는 DB 없이, 스모크 테스트는 실제 DB(읽기 전용, 롤백)로 돌며 DB가 없으면 건너뜁니다. 이 작업의 테스트 31건, 전체 205건.

| 파일 | 내용 |
| --- | --- |
| `tests/modules/master_data/test_sku_repository.py` | 두 문장 실행과 파라미터, SQL 조건(원천 `VALUES`·`CROSS JOIN`, `MAPPED` 조인, `jsonb_agg` 순서, `COALESCE(…, 'UNMAPPED')`, `BOOL_OR(코드 IS NULL)`, `hs10`, `split_part`, OPEN·INVENTORY, 정렬 동점), 행 → `SkuListRow`, 문자열 JSON과 누락 원천 `None`, 건수 None, LIKE 이스케이프 |
| `tests/modules/master_data/test_sku_repository_sql_smoke.py` | 빈 고객사 0건, 정렬·건수·범위 밖 페이지·`onlyErrors`, 모든 행이 원천 상수 순서로 항목을 갖고 `has_unmapped` = 코드 `None` 항목 유무, `hs10` 존재, 검색 부분 일치, 합성데이터 B사 SKU-201 = 재고 이슈 열림·두 원천 `MAPPED` |
| `tests/modules/master_data/test_sku_detail_sql.py` | 상세 SQL(실제 DB, 롤백): 없는 상품·다른 고객사, 상세 행 = 목록 행, 열린 이슈만, 영향 지연의 조건(닫힌 지연·이전 버전·보류 아님·선적에 없는 상품은 제외, 같은 SKU 줄이 여러 개여도 한 건), 매핑을 지운 `CODE_MISSING`, 앱 역할(RLS)에서도 읽힘 |
| `tests/modules/master_data/test_sku_list_service.py` | page → offset, 기본 페이지, 검색어 공백 처리, 건수 전달, 필드 옮김(`hs10` 포함), 누락 원천 → `CODE_MISSING` > `ISSUE_OPEN` > `OK` |
| `tests/modules/master_data/test_sku_api.py` | 401, OWNER·ADMIN 403, 파라미터 422 5종, CLIENT 200과 camelCase·공통 목록 모양(`hs10`, 누락 원천의 `externalCode: null`·`UNMAPPED`·`CODE_MISSING`), 기본값·파라미터 전달, RLS SQL 실행 |

로컬 DB의 합성데이터(2026 H1 v6.2 묶음)로 스모크 테스트를 실행해 B사 SKU-201이 `ISSUE_OPEN`으로 나오는 것을 확인했습니다.
매핑 누락 케이스는 롤백 트랜잭션 안에서 SHIPMENT 매핑 행 하나를 지우고 조회해, 해당 원천이 `externalCode = null`·`UNMAPPED`로 내려오고 `onlyErrors=true`에 잡히는 것을 확인했습니다(스모크 테스트는 읽기 전용이라 이 확인은 수동).

## 6. 아직 없는 것

| 기능 | 비워 둔 자리 |
| --- | --- |
| 이슈 ↔ 상품 정식 연결 | `data_validation_issues`에 `sku_id`·`warehouse_id` 추가(마이그레이션·수집기·합성데이터). 리포지토리의 `split_part`를 조인으로 교체 |
| 검증 이슈 목록·하단 배너 | 명세 4.3 `GET /validation-issues` (`affectedDelayEvents[]`) |
| 영향 지연의 원인 판별 | `affectedDelayEvents`는 포함 여부만 봅니다. 보류 원인을 상품 단위로 가리려면 실패 요약에 SKU를 구조화해 남겨야 합니다 |
| 창고 목록 | 명세 4.3 `GET /warehouses`. `ExternalCodeResponse`를 그대로 재사용 |
| 상품 상태 필터·정렬 선택 | 현재 INACTIVE 포함, `skuCode` 정렬 고정 |

## 7. 관련 파일

| 파일 | 역할 |
| --- | --- |
| `app/modules/master_data/presentation/sku_api_router.py` | `GET /api/v1/skus`, `GET /api/v1/skus/{skuId}`, 쿼리 파라미터 |
| `app/modules/master_data/presentation/sku_schemas.py` | `ExternalCodeResponse`, `SkuListItemResponse`, `SkuListResponse(Page)`, `SkuDetailResponse`, `SkuIssueResponse`, `AffectedDelayEventResponse` |
| `app/modules/master_data/application/sku_list_service.py` | `validationStatus` 판정, 검색어 정리, 행 → 항목, 상세 조립 |
| `app/modules/master_data/application/exception/sku_exceptions.py` | `SkuNotFoundException` (`SKU_NOT_FOUND`) |
| `app/modules/master_data/infrastructure/repository/sku_repository.py` | 목록·건수 SQL(원천 × SKU에 `MAPPED` 매핑 LEFT JOIN), `SkuListRow`, `ExternalCodeRow`, LIKE 이스케이프 |
| `app/modules/master_data/infrastructure/master_data_enums.py` | `PopularityTier`, `SkuStatus`, `MappingStatus` |
| `app/modules/master_data/infrastructure/sku_code_sources.py` | `SKU_CODE_SOURCE_DOMAINS` (SKU 코드를 쓰는 원천 `INVENTORY`·`SHIPMENT`, `externalCodes[]` 항목 순서) |
| `app/modules/master_data/dependencies.py` | `get_sku_list_service` 조립 |
| `app/core/pagination.py` | `PageParams`, `Page[T]` |
| `app/core/security/tenant.py` | `TenantContext`, `get_tenant_context` |
