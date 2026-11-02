# 프론트엔드에서 사용 가능한 API

개발이 끝나 프론트엔드에서 바로 호출해도 되는 API만 적습니다. 경로는 `/api/v1` 뒤에 붙습니다. 응답 모양은 `api_response_body_rule_glowship.md`를 따릅니다.

| 메서드 | 경로 | 설명 | 티켓 | 추가일 |
| --- | --- | --- | --- | --- |
| GET | `/delay-events` | 지연 목록·탭별 건수. 필터 `status`(`ALL`·`READY_FOR_REVIEW`·`ANALYZING`·`ON_HOLD`·`NEEDS_REANALYSIS`), `warehouseId`, `from`·`to`(UTC 날짜, `to` 포함), `q`, `hasFinalDecision`, `page`·`size`. 응답 `items`·`page`·`size`·`totalCount`·`totalPages`와 `counts`(키 `all`·`readyForReview`·`analyzing`·`onHold`·`needsReanalysis`, 탭·`hasFinalDecision` 필터 제외). 정렬은 `detectedAt` 최신순, 같으면 `delayEventId` | GLOW-16 | 11/02 |
| GET | `/delay-events/{delayEventId}` | 지연 상세·분석 실행 버전 이력(`runs[]`, 최신 버전 먼저). 다른 고객사 건은 404 | GLOW-16 | 11/02 |

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
