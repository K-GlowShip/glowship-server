# 데모 시드 (GLOW-89)

> 명세 v6.2 D12·C28. 코드 `scripts/seed_demo_runs.py`, 시험 `tests/modules/delay_analysis/test_demo_seed_sql.py`·`test_demo_seed_supplement_sql.py`.
>
> **데모는 보완 하루(7절)를 넣어 돌립니다(2026-11-09).** 그러면 결과가 아래 1절 표와 달라집니다(7절 표). 1~6절은 보완 없이 돌린 기본 시드입니다.

## 1. 왜 필요한가

확정 DB 는 지연 8건이 모두 닫혀 있어 판단 API 18(초안)·19(최종 선택)·20(재분석)이 `EPISODE_CLOSED`로 거부됩니다. 시드는 데모의 "지금"(7월 초 가상 시각)에 수동 수집을 한 번 해 **열린 지연 3건**을 만들고, 지연마다 **선택 대기(`READY_FOR_REVIEW`) 실행**을 엔진으로 계산해 넣습니다.

| 선적 | 단계 | 도착 창고·SKU | 계획 → 예상 3PL 입고 | 결과 | 대응안 |
| --- | --- | --- | --- | --- | --- |
| `DEMO-0702-PORT` | 항구 도착 후 | WH-EAST · SKU-018 | 06-30 → 07-25 | `READY_FOR_REVIEW` · `FORECAST_ONLY_READY` | 타 지역 출고 `FEASIBLE`(추천), 현행 유지 `FEASIBLE` |
| `DEMO-0702-PRE` | 출항 전 | WH-EAST · SKU-022 | 07-09 → 09-01 | 같음 | 현행 유지 `FEASIBLE`(추천), 한국 항공 보충 `NOT_FEASIBLE`(`NO_KR_ELIGIBLE_STOCK`), 타 지역 출고 `NEEDS_CHECK`(`DONOR_NO_NEXT_INBOUND`) |
| `DEMO-0702-SEA` | 해상 | WH-WEST · SKU-019 | 07-03 → 09-22 | 같음 | 같음 |

- **위험 주문(`READY_ORDER_RISK`)은 없습니다.** 확정 DB 의 미출고 주문은 약속일이 모두 07-03~07-07 이고 그때는 재고가 있어, 지연으로 늦어지는 주문이 생기지 않습니다. 대시보드 `atRiskOrderCount` 0, 실행 상세 위험 주문 표가 비는 것은 정상입니다. 보완은 주문·재고(예약 수량)가 맞는 하루치 데이터가 있어야 해서 7절 보완 하루로 넣습니다.
- 실행 시각은 7월 2일(고객사 마지막 적재 수집 시작, 이슈 #50), 엔진 기준일 D 는 07-01(최신 재고일 + 1)입니다.

## 2. 하는 일

1. **수동 수집:** API 25 와 같은 `ManualSyncService`로 시나리오의 그날 응답을 적재합니다(07-02 00:03:00Z). 수집이 지연을 판정해 에피소드를 엽니다. 시나리오 선적이 이미 있으면 건너뜁니다.
2. **실행:** 열린 지연마다 현재 실행이 없으면 `create_analysis_run`(AUTO, `EPISODE_STARTED`, 원인 스냅샷 = 감지 스냅샷, 실행 시각 = 고객사 마지막 적재 수집 시작)으로 만듭니다. 진행 중 실행이 있으면 그것을 쓰고, 이미 결과가 있으면 건너뜁니다.
3. **엔진 계산:** 엔진 입력 저장소(PR #36) → `check_input_seq` → `snapshot.build`(D = 고객사 최신 재고일 + 1) → `pipeline.analyze` → 설명(`generate_explanation`, PR #45, 결정적 mock 모델). 에이전트·LLM 을 부르지 않아 결과가 매번 같습니다.
4. **저장(L2):** 결과 표 4개(`inventory_impact_results`·`at_risk_orders`·`response_options`·`option_order_outcomes`) 행은 엔진 `storage.rows_for`로 만듭니다(확정 DB overlay 를 만든 함수, 행 모양이 같음). 실행에는 상태·결과 분류·의존 범위·판정 서명·설명과 완료 시각(실행 시작 + 1초, 결과 저장과 같은 규칙)을 쓰고, 데이터 버전을 기록합니다(서버 결과 저장과 같이 저장 때, 2026-11-11. 데모 실행 3건 모두 5건).

- **서버 결과 저장(`save_analysis_result`)을 쓰지 않는 이유:** 그 함수는 에이전트 도구 출력(T6·T7·T10·T11)을 받는데, 엔진 결과를 그 모양으로 바꾸는 코드가 아직 없습니다(에이전트 도구 범위). 분석 자동 시작(#51)이 들어오면 시드도 그 경로로 바꿀 수 있습니다.
- 엔진 보류(`Hold`)나 설명 실패가 나면 아무것도 남기지 않고 멈춥니다(데모에 쓸 수 없음).

## 3. 실행

확정 DB 적재(`00_필수_최신DB_전체재적재`)와 `alembic upgrade head` 뒤, `glowship-server` 폴더에서:

1. 엔진 폴더: `docs/analysis/engine-local-path.md` 준비 절대로 만든 폴더를 `GLOWSHIP_ENGINE_DIR`에 넣습니다.
2. 시나리오 파일: 아래 JSON 을 파일로 저장하고 `VIRTUAL_TMS_SCENARIO_PATH`에 넣습니다. `VIRTUAL_CLOCK_START`는 기본값(`2026-07-02T00:00:00Z`)을 둡니다.
3. 미리 보기(되돌림): `uv run python -m scripts.seed_demo_runs --dry-run`
4. 적재: `uv run python -m scripts.seed_demo_runs`

```json
{
  "interface": "GET /v1/shipments",
  "responses": [
    {
      "fetched_at": "2026-07-02T00:00:00Z",
      "_demo_company": "A_BEAUTY",
      "data": [
        {"shipmentNo": "DEMO-0702-PRE", "mode": "AIR", "destWh": "TMS-EWR", "plannedKrRelease": "2026-07-03", "stage": "PRE_DEPARTURE",
         "etaPort": null, "eta3pl": "2026-09-01", "actual3pl": null, "items": [{"lineNo": 1, "sku": "TMS-SKU-022", "qty": 96}]},
        {"shipmentNo": "DEMO-0702-SEA", "mode": "OCEAN", "destWh": "TMS-LAX", "plannedKrRelease": "2026-06-08", "stage": "AT_SEA",
         "etaPort": "2026-09-15", "eta3pl": "2026-09-22", "actual3pl": null, "items": [{"lineNo": 1, "sku": "TMS-SKU-019", "qty": 96}]},
        {"shipmentNo": "DEMO-0702-PORT", "mode": "AIR", "destWh": "TMS-EWR", "plannedKrRelease": "2026-06-24", "stage": "AT_PORT",
         "etaPort": "2026-06-29", "eta3pl": "2026-07-25", "actual3pl": null, "items": [{"lineNo": 1, "sku": "TMS-SKU-018", "qty": 96}]}
      ]
    }
  ]
}
```

여러 번 돌려도 같습니다(선적이 있으면 수집을 건너뛰고, 결과가 있는 실행은 건너뜀).

## 4. 확인 (적재 뒤)

1. `GET /delay-events?status=READY_FOR_REVIEW` → 3건(A_BEAUTY 계정)
2. `GET /analysis-runs/{runId}`(API 17) → 재고 영향·대응안·설명
3. 팀원 계정 `POST /analysis-runs/{runId}/drafts` → 201
4. 팀장 계정 `POST /analysis-runs/{runId}/final-decision`(`FEASIBLE` 대응안, `Idempotency-Key`) → 201

## 5. 되돌리기

시드는 확정 DB 위에 수집 1회와 실행 3건(+결과 행)을 더합니다. 처음 상태로 돌리려면 공유 묶음 `00_필수_최신DB_전체재적재`로 다시 적재합니다. 서버를 띄운 채 다시 적재했으면 서버를 다시 시작합니다(엔진 저장소 캐시가 다시 적재하기 전의 입력을 줄 수 있음, `engine-store-cache.md` 5절). 수집·변경 기록·적용 순번까지 되돌려야 해서 시드만 지우는 스크립트는 두지 않았습니다.

## 6. 시험

| 파일 | 확인하는 것 |
| --- | --- |
| `test_demo_seed_sql.py` | 롤백 트랜잭션·실제 엔진: 3건 `READY_FOR_REVIEW`·AUTO·열린 지연, 실행 날짜 07-02·완료 = 시작 + 1초, 설명 `PASSED`, 결과 표 행, 대응안 판정 3종·추천 1개씩, 두 번 돌려도 새로 만들지 않음. `GLOWSHIP_ENGINE_DIR`이 없거나 확정 DB 가 아니면 건너뜀 |
| `test_demo_seed_supplement_sql.py` | 보완 하루(7절): 원천별 레코드, 작은 보완으로 수집 5건(07-02 00:00~00:04 자리·`SUCCEEDED`·건수)과 재실행 건너뜀. `DEMO_SUPPLEMENT_PATH`(보완 파일)가 있으면 끝까지 돌려 7절 결과(해상 `READY_ORDER_RISK`·지연 때문 위험 주문 3건·실행 날짜 07-03)를 확인 |

## 7. 보완 하루: 위험 주문 (2026-11-09)

기본 시드는 위험 주문(`READY_ORDER_RISK`)이 없습니다(1절). 확정 데이터가 06-30 에서 끝나 그 뒤 주문이 없기 때문입니다. 그래서 census 규칙으로 **07-01 하루**(주문·재고)를 만들어, 데모 수집 전에 관측일 07-02 수집으로 넣습니다. 설계: K-GlowShip `설계/GLOW-89_위험주문_데이터보완_계획.md`.

### 7-1. 보완 하루 만들기 (census)

`census_kbeauty/tools/synth2026/extend_day.py <DB> <컨테이너> <계정> A_BEAUTY 8 18 <출력 폴더> WH-WEST SKU-019`

- 06-30 마감 상태는 확정 DB 에서 엔진과 같은 방법(`snapshot.ReplayReservationSource.restore`)으로 복원하고, 07-01 일반 주문은 생성기(`ledger2026`)와 같은 규칙·키로, 배정은 공유 모듈 `allocation_core`(v5)로 한다. 같은 방법으로 06-30·06-15 를 다시 계산하면 확정 DB 재고와 모든 행이 같다(입고가 없는 날).
- 사건 주문: SEPHORA 8건 × 18개, WH-WEST·SKU-019, 약속 07-08. 07-01 마감에 그 창고 SKU-019 는 106개가 모두 예약(가용 0).
- 결과 파일: ORDER 응답(새 주문 + 07-01 출고 `SHIPPED`·배송 완료 `DELIVERED`로 바뀐 주문, ERP 내부 코드), INVENTORY 응답(07-01 모든 창고·SKU, WMS 매핑 코드), 검사(배정 불변식·가용 ≥ 0·코드 중복 없음·엔진 D = 07-02 재생 예약 일치).

### 7-2. 시드에 넣기

`uv run python -m scripts.seed_demo_runs --supplement <보완 파일>` (먼저 `--dry-run`)

- 데모 수집 전에 07-02 가상 시각 원천 자리(상품 00:00·창고 00:01·주문 00:02·선적 00:03·재고 00:04, 완료 +30초)에 다섯 원천 수집을 넣는다. 상품·창고·선적은 변경 없음(빈 응답).
- 그래서 데모 SHIPMENT 수동 수집은 **07-03 00:03:00Z** 가 된다. 시나리오 파일의 `fetched_at`도 `2026-07-03T00:00:00Z`로 둔다(3절 JSON 에서 날짜만 바꿈).
- 실행 시각 07-03 00:03(실행 코드 `AN-260703-…`), 엔진 기준일 D = 최신 재고일(07-01) + 1 = 07-02.

### 7-3. 결과

| 선적 | 결과 | 대응안 | 위험 주문 |
| --- | --- | --- | --- |
| `DEMO-0702-SEA` 해상 · WH-WEST · SKU-019 | `READY_FOR_REVIEW` · **`READY_ORDER_RISK`** | 현행 유지 `FEASIBLE`(추천), 한국 항공 보충 `NOT_FEASIBLE`, 타 지역 출고 `NEEDS_CHECK` | 3건(SEPHORA 각 18개): 약속 07-08, 예정대로면 07-06, 지연 반영 07-31 → `DELAY_INDUCED` |
| `DEMO-0702-PORT` 항구 · WH-EAST · SKU-018 | `READY_FOR_REVIEW` · `FORECAST_ONLY_READY` | 현행 유지 `FEASIBLE`(추천), 타 지역 출고 `NOT_FEASIBLE`(`FORECAST_EFFECT_BELOW_ONE_UNIT`) | 없음 |
| `DEMO-0702-PRE` 출항 전 · WH-EAST · SKU-022 | `NO_RISK` · `PURE_NO_RISK` | — | 없음 |

- **화면 숫자(2026-11-10, 데모 DB `glowship_demo`를 API 로 읽음, 이슈 #103):** 지연 목록 `atRiskOrderCount` 해상 3·항구 0·출항 전 0, 대시보드 `atRiskOrderCount` 3(`atRiskOrderRate` 0.0168, `lastSyncedAt` 07-03 00:03:30). 현재 실행의 위험 주문은 해상 `DELAY_INDUCED` 3행뿐이고 `PRE_EXISTING`은 0행이다(같은 DB 의 `PRE_EXISTING` 7행은 01-15 에 만든 `STALE` 실행이라 숫자에 들어가지 않는다). 끝까지 시험이 항구·출항 전 0행을 확인한다. 보완 하루를 건너뛰면(이미 넣었거나 파일 날짜가 맞지 않음) 시드가 "보완 하루 건너뜀"을 한 줄 출력한다.
- 기준일이 하루 뒤(07-02)라 항구·출항 전 결과가 기본 시드와 다르다(사건 주문 없이 보완 하루만 넣어도 같음). 판단 API 18·19 대상은 해상·항구 2건이다.
- 사건을 항구 선적(SKU-018)에 두면 위험 주문이 나오지 않는다: 그 선적의 계획 입고일(06-30)이 기준일보다 일러, 계획대로였어도 늦는 주문(`PRE_EXISTING`)이 되기 때문이다(엔진 `core.order_layer`, 결과 분류는 `DELAY_INDUCED` 가 있어야 `READY_ORDER_RISK`). 그래서 계획 입고일(07-03)이 기준일 뒤인 해상 선적 SKU 에 둔다.
