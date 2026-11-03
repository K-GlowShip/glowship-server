# 프론트엔드에서 사용 가능한 API

개발이 끝나 프론트엔드에서 바로 호출해도 되는 API만 적습니다. 경로는 `/api/v1` 뒤에 붙습니다. 응답 모양은 `api_response_body_rule_glowship.md`를 따릅니다.

| 메서드 | 경로 | 설명 | 티켓 | 추가일 |
| --- | --- | --- | --- | --- |
| POST | `/auth/signup` | Glowship 내부 운영자(ADMIN) 계정 생성. 인증 없음. 요청 `{email, password}`(`password`는 Base64 인코딩, 디코딩 결과 영문·숫자 6~16자). 성공 201, 본문 없음. 400(비밀번호 형식)·409(중복 이메일)·422. 고객사 계정은 이 API 가 아니라 `/admin/users`로 만든다(명세 D5). [문서](auth/login.md) | GLOW-71, GLOW-94 | 2026-10-29 (경로 `/auth` 하위로 변경 2026-11-02) |
| POST | `/auth/login` | 로그인. 요청 `{email, password}`(`password`는 Base64 인코딩). 성공 200, 본문 없음. 토큰은 `Set-Cookie`로만 전달: `access_token`(HttpOnly, Path=/)·`refresh_token`(HttpOnly, Path=/api/v1/auth/refresh). 프론트는 토큰을 읽지 않고 요청에 `credentials: "include"`만 붙인다. 400(Base64 아님)·401(이메일 또는 비밀번호 불일치)·403(비활성 계정)·422. 재발급·로그아웃은 미구현. [문서](auth/login.md) | GLOW-79, GLOW-94 | 2026-10-30 (경로 `/auth` 하위로 변경 2026-11-02) |
| POST | `/admin/users` | 고객사 계정 생성. ADMIN 전용(`access_token` 쿠키). 요청 `{companyId, email, password(Base64), roleCode(CLIENT_MEMBER·CLIENT_LEAD), displayName?}`. 응답 201 `{userId, companyId, email, displayName, roleCode, status, createdAt}`. 요청자가 그 고객사에 배정되어 있고 계정 관리 권한이 있어야 한다. 400(비밀번호 형식)·401·403(ADMIN 아님·미배정·권한 없음)·404(고객사 없음)·409(중복 이메일·비활성 고객사)·422. [문서](admin/client-account.md) | GLOW-85 | 2026-10-30 |
| GET | `/data-sync-runs/latest` | 원천 유형(ORDER·INVENTORY·SHIPMENT·PRODUCT·WAREHOUSE)별 마지막 수집 실행·미해결 이슈 수·원천 시스템 이름·수집 주기와 서버 판정 `isSynced`·`lastSyncedAt` (명세 v6.2 4.3). 고객사 사용자 전용. [문서](data-sync/latest-sync-status.md) | GLOW-97 | 2026-11-02 |
| GET | `/delay-events` | 지연 목록·탭별 건수. 필터 `status`(`ALL`·`READY_FOR_REVIEW`·`ANALYZING`·`ON_HOLD`·`NEEDS_REANALYSIS`), `warehouseId`, `from`·`to`(UTC 날짜, `to` 포함), `q`, `hasFinalDecision`, `page`·`size`. 응답 `items`·`page`·`size`·`totalCount`·`totalPages`와 `counts`(키 `all`·`readyForReview`·`analyzing`·`onHold`·`needsReanalysis`, 탭·`hasFinalDecision` 필터 제외). 정렬은 `detectedAt` 최신순, 같으면 `delayEventId` | GLOW-16 | 2026-11-02 |
| GET | `/delay-events/{delayEventId}` | 지연 상세·분석 실행 버전 이력(`runs[]`, 최신 버전 먼저). 다른 고객사 건은 404 | GLOW-16 | 2026-11-02 |
| GET | `/orders` | 주문 목록. 필터 `status`(주문 상태 6종), `warehouseId`, `salesChannel`(`AMAZON`·`SEPHORA`), `from`·`to`(ISO 8601 UTC 시각, `[from, to)`로 `to` 미포함), `q`(주문 번호·SKU 코드·상품명), `page`·`size`. 행마다 `warehouse` 요약. 행마다 주문 상세 `lines[]`(`lineNo`, `sku` 요약, `orderedQuantity`, `shippedQuantity`, `lineStatus`, `lineNo` 순, 없으면 `[]`). 정렬은 `orderedAt` 최신순, 같으면 `orderId`. 출고 날짜 4개 중 `actualDispatchDate`는 수집 변경 기록으로 역산(출고 전이면 `null`), `securedDate`·`plannedDispatchDate`·`expectedDeliveryDate`는 아직 계산하지 않아 항상 `null`. 고객사 사용자 전용. [문서](order/order-list.md) | GLOW-109 | 2026-11-03 |
| GET | `/skus` | 상품(SKU) 목록. 필터 `q`(상품명·ERP 코드), `onlyErrors`, `page`·`size`. 행마다 `hs10`, 원천별 외부 코드 `externalCodes[]`(INVENTORY·SHIPMENT 항목 고정, 매핑 없으면 `externalCode: null`·`UNMAPPED`)와 `validationStatus`(`OK`·`CODE_MISSING`·`ISSUE_OPEN`). 정렬은 `skuCode`, 같으면 `skuId`. 고객사 사용자 전용. [문서](master-data/sku-list.md) | GLOW-104 | 2026-11-03 |
| GET | `/inventory` | 재고 목록(한 일자의 창고·SKU별 재고). 필터 `date`(`YYYY-MM-DD`, 생략 시 고객사의 최신 적재 일자), `warehouseId`, `skuId`, `q`(SKU 코드·상품명), `page`·`size`. 응답 최상위에 기준 일자 `inventoryDate`(재고 없으면 `null`). 행마다 `warehouse`·`sku` 요약과 `onHandQuantity`(= `reservedQuantity` + `availableQuantity`), `outboundQuantity`, `dataSyncRunId`. 정렬은 창고 코드 → SKU 코드, 같으면 `skuId`. 고객사 사용자 전용. [문서](inventory/inventory-list.md) | GLOW-111 | 2026-11-03 |

**목록 API 공통:** 요청 `page`(1부터, 기본 1)·`size`(기본 20, 최대 100). 응답 `items`·`page`·`size`·`totalCount`·`totalPages` (`app/core/pagination.py`)

**연관 대상 요약 객체** (`app/core/summary_schemas.py`, 어느 API에서든 같은 모양)

| 대상 | 모양 |
| --- | --- |
| `shipment` | `{ shipmentId, shipmentCode }` |
| `warehouse` | `{ warehouseId, warehouseCode, regionCode }` |
| `representativeSku` | `{ skuId, skuCode, productName }` 또는 `null`(선적 상세 없음) |
| `currentRun` | `{ runId, runCode, status, resultClass }` 또는 `null`(실행 없음) |

**지연 목록·상세 필드 메모**

- `stage`·`planned3plInboundDate`·`expected3plInboundDate`·`delayDays`는 선적의 **현재 값**입니다. 회복되어 예상일 ≤ 계획일이면 `delayDays`는 0입니다. 상세의 `previousExpected3plInboundDate`는 감지 직전 관측(스냅샷)의 예상일입니다.
- `atRiskOrderCount`는 서로 다른 주문 수입니다. 저장된 계산 결과가 없으면 `null`(실행 없음·분석 중·`ON_HOLD`·`NO_DELAY`·도구 실패 `FAILED`)이고, 설명 생성만 실패한 `FAILED`는 숫자입니다.
- `closedAt`·`closeReason`(`RECOVERED`·`RECEIVED`)은 열린 지연이면 `null`입니다.

**아직 `null`로 오는 필드 (서버 후속 작업 후 값이 채워짐)**

| 필드 | 채워지는 시점 |
| --- | --- |
| `episodeNo`, `currentRun.resultClass`, `runs[].triggerReason`·`completedStatus`·`resultClass` | 개발자 3의 마이그레이션 0005 병합 후 |
| `currentRun.runCode`, `runs[].runCode` | 마이그레이션 0006(D14) 병합 후 |

**11/02 응답 변경 (이전 버전과 다른 점):** `shipmentCode` → `shipment.shipmentCode`, `representativeSku`(상품명 문자열)·`representativeSkuCode` → `representativeSku` 객체, `runId`·`runCode`·`runStatus` → `currentRun` 객체, 탭 `PENDING_DECISION` → `READY_FOR_REVIEW`, `counts.pendingDecision` → `counts.readyForReview`. 추가: `warehouse`, `transportMode`, `closedAt`·`closeReason`, `episodeNo`, `runs[]`의 `parentRunId`·`staleDetectedAt`·`stalePrimaryReason`·`triggerReason`·`completedStatus`·`resultClass`. 상세의 `shipment` 안에 있던 `transportMode`·`warehouseId`·`route`·대표 상품·`skuCount`는 목록과 같은 최상위 필드로 옮겼습니다.
