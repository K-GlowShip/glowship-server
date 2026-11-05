# 거점별 재고 현황

고객사 사용자가 홈 대시보드(SCR-002)의 **거점별 재고 현황** 표에서 보는 API가 현재 어떻게 동작하는지 설명합니다.
정의는 API 명세 v6.2 4.4 `GET /inventory-status`와 1절 D9, 0절 C22를 따릅니다(`glowship-plan` 저장소).

## 1. 무엇을 계산하나

- **최신 재고와 현재 운영 기준으로 조회할 때 계산하는 참고값**입니다. 저장하지 않습니다.
- **입고 예정(지연 입고 포함)은 반영하지 않습니다**(C22). 지연 입고를 반영한 값은 분석 결과(`GET /analysis-runs/{id}`)에만 있습니다. 화면은 이 값이 참고값임을 기준과 함께 표시합니다.
- 계산 정의는 분석 엔진(`glowship_engine` `snapshot.build`·`core.forecast_sku`)과 같습니다.

| 항목 | 정의 |
| --- | --- |
| 기준일 D | 고객사의 최신 재고 일자 + 1일. 응답 `inventoryDate` = D − 1 (엔진도 D − 1 재고를 씀) |
| 운영 기준 | `effective_from ≤ D`인 것 중 `version_no` 최대. 응답 `operatingRuleVersionNo`·`salesBasisDays`·`safetyStockDays` |
| 하루 판매량 μ | D 직전 `sales_basis_days`일(D − `sales_basis_days` ~ D − 1)의 **유효 주문 수요** 합 ÷ `sales_basis_days`(D9, `salesBasisType = ORDER_DEMAND`). 유효 주문 수요 = `CANCELLED`·`REMOVED_AT_SOURCE`가 아닌 주문의 `ACTIVE` 상세 `ordered_quantity`, 날짜는 `ordered_at`의 UTC 날짜, 창고는 주문의 출고 창고. 응답 `averageDailySales`는 엔진 저장값과 같게 소수 6자리 |
| 안전재고 | `ceil(μ × safety_stock_days)` (소수 4자리로 고정한 뒤 올림, 엔진 `ceil_i`) |
| 남은 수량 | D + k날 = `available_quantity − μ × (k + 1)` (D날 판매도 뺌). 소수 4자리(half-even)로 비교 |
| `safetyStockBreachDate` | 남은 수량이 안전재고보다 처음 작아지는 날 |
| `stockoutDate` | 남은 수량이 0 이하가 되는 첫날 |
| `status` | 하회일 ≤ D → `RISK`, D < 하회일 ≤ D + `safety_stock_days` → `CAUTION`, 그 밖 → `OK` |

- μ가 0이면(그 창고에서 팔리지 않는 SKU, 예: 한국 창고 WH-KR) 두 날짜 모두 `null`이고 `OK`입니다.
- μ가 아주 작아 날짜가 `9999-12-31`을 넘으면(예: 하루 1/28개에 가용 13만 개) 그 날짜는 `null`입니다(명세 C22). 하회일이 `null`이면 `OK`입니다.
- 계산 함수는 `app/modules/inventory/application/inventory_status_rules.py`(순수 함수)이고, 날마다 반복하지 않고 이분 탐색으로 날짜를 찾습니다. 테스트가 엔진의 하루씩 누적하는 반복과 무작위 1,000건에서 같은 날짜를 내는지 확인합니다.

## 2. 조회 API

`GET /api/v1/inventory-status` — `app/modules/inventory/presentation/inventory_status_api_router.py`

| 이름 | 타입 | 기본값 | 설명 |
| --- | --- | --- | --- |
| `warehouseId` | UUID | 없음 | 창고 |
| `page` | 정수 ≥ 1 | `1` | 페이지 번호 |
| `size` | 정수 1~100 | `20` | 페이지 크기 |

- 정렬: `status` 위험 순(`RISK` → `CAUTION` → `OK`), 같으면 `stockoutDate` 빠른 순(`null`은 뒤), 같으면 `skuId`, 같은 SKU가 여러 창고에 있으면 `warehouseId`. 계산한 상태로 정렬하므로 모든 행을 계산한 뒤 페이지를 자릅니다(고객사 한 곳은 창고 × SKU, 확정 DB 최대 66행).
- 적재된 재고나 적용할 운영 기준이 없으면 `items: []`, 계산 기준 필드는 `null`입니다.

### 성공 응답 (확정 DB, A_BEAUTY, 첫 행)

```json
{
  "items": [
    {
      "warehouse": { "warehouseId": "…", "warehouseCode": "WH-EAST", "regionCode": "EAST" },
      "sku": { "skuId": "…", "skuCode": "SKU-020", "productName": "…" },
      "availableQuantity": 10,
      "averageDailySales": 1.964286,
      "safetyStockBreachDate": "2026-07-01",
      "stockoutDate": "2026-07-06",
      "status": "RISK"
    }
  ],
  "page": 1,
  "size": 20,
  "totalCount": 66,
  "totalPages": 4,
  "inventoryDate": "2026-06-30",
  "operatingRuleVersionNo": 1,
  "salesBasisDays": 28,
  "safetyStockDays": 14,
  "salesBasisType": "ORDER_DEMAND"
}
```

- 확정 DB 상태 분포: A_BEAUTY 66행(`RISK` 3·`CAUTION` 34·`OK` 29), B_COSMETICS 44행(`RISK` 7·`CAUTION` 16·`OK` 21).
- 위 행: 안전재고 `ceil(1.964286 × 14)` = 28, D(07-01)날 남은 수량 10 − 1.964286 = 8.0357 < 28 → `RISK`, 남은 수량이 0 이하가 되는 첫날 07-06.

## 3. 오류

| 상태 | `code` | 경우 |
| --- | --- | --- |
| `401` | `UNAUTHENTICATED` | 쿠키 없음·만료 |
| `403` | `FORBIDDEN` | 토큰에 `company_id` 없음(OWNER·ADMIN) |
| `422` | `VALIDATION_ERROR` | `warehouseId` UUID 형식 오류, `page < 1`, `size` 범위 밖 |

## 4. 테스트

| 파일 | 내용 |
| --- | --- |
| `tests/modules/inventory/test_inventory_status_rules.py` | 손으로 센 경우, 경계(하회일 = D + 일수 → `CAUTION`), 판매 0, 엔진 반복과 1,000건 일치 |
| `tests/modules/inventory/test_inventory_status_service.py` | 기준일·판매량 기간, 정렬·페이지, 소수 6자리, 빈 결과 |
| `tests/modules/inventory/test_inventory_status_api.py` | 401·403·422, 응답 필드 순서·camelCase |
| `tests/modules/inventory/test_inventory_status_sql.py` | 엔진 저장값(`inventory_impact_results` `ORDER_DEMAND` 391행, 기간 15개)과 하루 판매량·안전재고 일치, 확정 DB 66행·운영 기준 1번 |
