# 평가 시나리오 10건 정답 대조 — 1단계 (GLOW-58)

GLOW-50 에서 만든 평가 시나리오 10건(`Data/평가시나리오_검토용/`)을 서버 쪽에서 돌려 **수작업 정답표**와 맞춰 본 기록입니다.
기대값의 출처는 `02_시나리오와정답.md`(3절 요약표·5절 시나리오별 표)이고, 엔진 fixture 의 `EXPECTED` 는 쓰지 않았습니다.
시험은 모두 `tests/modules/delay_analysis/evaluation/` 에 있고 운영 코드는 바꾸지 않았습니다.

## 1. 범위와 근거

- **1단계(이 문서, 11-06):** 엔진 묶음의 `glowship_engine/tests/fixture_glow50.py` 가 만드는 시나리오 세계(snap)와 엔진 판정(analysis)을 입력으로 쓴다.
  - 서버 T9 `check_option_feasibility`·T11 `compare_options` 에 그 입력을 넣어 정답표와 견준다(어댑터 `evaluation_adapters.py`).
  - 엔진 ①~⑤ 값(소진일·raw·정수 준비량·품절일·위험 주문 귀속·지연 증분 회복·추천·H_end)과 대응안 값(공급량·확정 감소·회복 배송일·required_quantity)을 정답표와 견준다.
  - GS-04 배정, GS-06 보류, GS-09 서명 비교, GS-10 후속 실행, GS-11 공여 배정 탐침은 엔진 fixture 함수로 돌려 정답표와 견준다.
  - 엔진 자체 검사(`fixture_glow50.main`, 계산 31건 + 저장 21건)를 서버 시험 안에서 돌린다. 저장 검사는 이 세션 DB(`glowship-db_3`)에 넣고 ROLLBACK 한다.
  - AI 설명 생성(GLOW-127 `generate_explanation`, mock 모델)과 T12 재검증, GS-06 보류의 서버 저장 모양(`hold_summary`)을 본다.
- **2단계(나중):** 엔진 입력 저장소(GLOW-125 2부)·T6·T7(GLOW-126)·T10·그래프(GLOW-129) 위에서 같은 10건을 DB 입력으로 돌리고 통합 시험 T13·T20 으로 잇는다.
- **근거 문서:** 도구 계약 `docs/agent-flow/tool-contracts.md` 5·7절(T9·T11), 설명 `docs/agent-flow/explanation.md`, 결과 저장 `docs/analysis/result-save.md`, 정답표 `02_시나리오와정답.md`, 통과 조건 `03_통과조건과근거.md`.

## 2. 시험 구성

| 파일 | 보는 것 |
| --- | --- |
| `conftest.py` | 엔진 fixture 불러오기(`GLOWSHIP_ENGINE_DIR` 없으면 전부 건너뜀. 엔진 시험 파일은 시험 목록을 대조하는 `engine_test_modules.load_engine_test_module`로 부름, `docs/analysis/engine-local-path.md` 3절), 시나리오 캐시(`scenario(key)`). GS-05 는 기존 `fixture_gs05` 세계, `GS-11-probe` 는 공여 보유 200 탐침 |
| `evaluation_adapters.py` | 엔진 snap → `CheckOptionFeasibilityInput`(리드타임 키 7개 이름 바꿈, `next_inbound` 없는 SKU 는 `null`), 엔진 options → `CompareOptionsInput`(엔진 `supply_date` → `expected_delivery_date`), 행 모양 펴기 |
| `test_evaluation_t9_t11.py` | 9건(GS-02·03·05·07·08·09A·09B·10·11) T9 후보 행·사전 판정·공급일·긴급 항공 SKU, T11 순위·추천, 엔진 대조(공급일·rank), ①~⑤·대응안 값, GS-11 탐침(O2 만 회복) |
| `test_evaluation_gs03_options.py` | 음성: KR 재고 0 → T9 가 NO_SUPPLY, KR NOT_FEASIBLE → T11 이 KEEP 1위. 같은 입력 = 같은 서명 |
| `test_evaluation_gs04_allocation.py` | GS-04 D−1 배정(확보 집합·대기·보호량·대기 원인·보유·예약·확보일·출고일) |
| `test_evaluation_gs06_hold.py` | GS-06 엔진 Hold 요약(ON_HOLD·VALIDATING·REQUIRED_DATA_INVALID·1·false·의존 범위 없음)과 대조군 |
| `test_evaluation_hold_shape.py` | GS-06 → 서버 `hold_summary(...).columns()` 가 analysis_runs 열과 같고, 보류 요약으로 결과 행 저장은 거부 |
| `test_evaluation_gs09_signature.py` | GS-09 변경 A(STALE·RELEVANT_ORDER_CHANGED)·B(유지), 서명 다름/같음, R1 = GS-03 |
| `test_evaluation_gs10_followup.py` | GS-10 트리거 2개, AUTO/SCHEDULE_WORSENED, D' 03-11·E' 04-06, R1 STALE DELAY_SCHEDULE_CHANGED, 재생 예약 50 |
| `test_evaluation_engine_selfcheck.py` | 엔진 자체 검사 31건(결과 파일은 임시 폴더), DB 저장 검사 52건(ROLLBACK, 거부 0, 행 수 그대로), 음성(CHECK 위반 행 거부) |
| `test_evaluation_explanation.py` | READY 9건 설명 생성 → T12 재검증 PASSED 기대(8건 strict xfail, 4.1절), 원인 고정, 지금 동작(#28 explanation_failure 모양) |

실행: `uv run pytest tests/modules/delay_analysis/evaluation`. 저장 검사는 `docker` 와 컨테이너 `glowship-db` 의 자기 DB(`.env` POSTGRES_*)가 있어야 하고 없으면 건너뜁니다. 11-06 결과: **79 통과, 8 xfail**(전체 2236 통과·2 건너뜀·8 xfail).

## 3. 정답표 대조 결과 (11-06)

| 시나리오 | 정답표 항목 | 결과 |
| --- | --- | --- |
| GS-02 출항 전·PDA | T9 KEEP·PDA(03-20)·OTHER 3행(KR 행 없음), T11 PDA 1★·KEEP 2, ①~⑤, PDA 400/70/03-23/270 | 모두 일치 |
| GS-03 항구 도착·KR | T9 KEEP(03-30)·KR(03-17, CREAM20)·OTHER(03-10) 모두 CALCULATE, T11 KR 1★·KEEP 2·OTHER NULL, ①~⑤, KR 200/100/03-20/200, 저장 통합(CHECK·FK) | 모두 일치. 저장은 run_code 를 넣어야 통과(4.2절) |
| GS-04 배정 | 확보 {O-A,O-C,O-H}, 출고 03-11, 대기 3건·보호량·원인, 보유·예약·가용 | 모두 일치(대기 원인은 DIRECT·BUNDLED 앞글자만) |
| GS-05 복수 SKU | K_IMPACTED {X,Y}, T9 KR 긴급 항공 SKU [X], T11 KR 1★, X·Y ①~③, H_end 04-13, KR 200/120/03-25/224 | 모두 일치 |
| GS-06 자료 부족 | ON_HOLD·VALIDATING·REQUIRED_DATA_INVALID·1·false, 결과·서명 없음, 대조군 reserved 50 | 엔진·서버 열 모두 일치 |
| GS-07 다음 입고 없음 | T9 OTHER NEEDS_CHECK DONOR_NO_NEXT_INBOUND(기준일 03-10), T11 KR 1★·OTHER NULL, OTHER 집계 NULL·주문별 결과 0행 | 모두 일치(저장 검사에서도 NULL 그대로) |
| GS-08 대응 불가 | T9 KR NO_SUPPLY NO_KR_ELIGIBLE_STOCK(03-17), T11 KEEP 1★, KR 집계 0·잔여 60·O1 NOT_RECOVERED | 모두 일치 |
| GS-09 서명 | A: RELEVANT_ORDER_CHANGED·서명 다름·STALE, 재계산 110.7143/221.4286/110.7143·111·111·222·110·회복 2; B: RELEVANT_INVENTORY_CHANGED·서명 같음·유지 | 모두 일치(엔진 1차 필터·서명. 서버 `StaleJudgmentService` 는 2단계) |
| GS-10 후속 실행 | 트리거·AUTO/SCHEDULE_WORSENED·D' 03-11·E' 04-06·R1 STALE DELAY_SCHEDULE_CHANGED, 새 실행 소진일 03-11·90/260/170·KR 260/170/03-21 | 모두 일치 |
| GS-11 기존 위험 + OTHER | O2 PRE_EXISTING·O1 DELAY_INDUCED, T11 KR 1★·OTHER 2·KEEP 3, KR·OTHER 전체 회복 2, OTHER 180/70/잔여 30/03-16, 탐침(공여 200) O2 만 회복·지연 증분 0 | 모두 일치 |
| AI 설명(READY 9건) | 설명 통과 + T12 PASSED | **GS-05 만 통과.** 나머지 8건은 엔진 검증기 문제로 3번 모두 실패(4.1절) |

## 4. 발견 (엔진·다른 담당 몫, 결정 대기)

### 4.1 설명 검증기가 구분자 없는 SKU 코드 안의 숫자를 수치로 센다 — 평가 fixture 설명이 실패 (11-07 범위 바로잡음)

- **현상:** 엔진을 직접 불러도(서버 감싸기 없이) GS-02·03·07·08·09A·09B·10·11 의 mock 설명이 3번 모두 `VALUE_MISMATCH@risk_summary#s2` 로 실패합니다. GS-05(SKU `X`·`Y`)만 통과합니다.
- **원인:** `explain/textcheck.tokens` 가 "SKU별 지연 증분은 CREAM20 100개입니다" 에서 숫자를 `[20, 100]` 으로 셉니다. 문장이 인용한 값은 `[100]` 하나라 다중집합이 달라집니다(`validator.py` 의 1:1 대응 규칙). `CREAM20` 은 코드 토큰으로도 잡히지 않습니다. 코드 토큰 규칙(`textcheck.py` 12행 `CODE`)은 `-`·`_` 로 이어진 대문자 코드만 코드로 빼내므로, `SKU-005 31개` 는 `[31]` 로 맞게 셉니다. 숫자를 잘못 세는 것은 `CREAM20`·`SKU005` 처럼 구분자 없이 영문 뒤에 숫자가 붙은 코드뿐입니다.
- **영향:** 엔진 평가 fixture(GLOW-50)의 SKU 코드(`CREAM20`·`TONER50` …)로 만든 설명이 엔진 자기 검증기에 걸려, 평가 시나리오에서 설명 통과를 확인할 수 없습니다. 실제 DB 의 SKU 코드(`SKU-001` 모양, 구분자 있음)는 해당하지 않습니다 — overlay 실행 13건이 `SKU-005`·`SKU-026` 같은 코드로 설명 PASSED 로 저장돼 있습니다. 운영 SKU 코드가 구분자 없는 모양이면 같은 실패가 납니다.
- **바로잡음(11-07):** 11-06 판은 "실제 DB SKU 코드도 해당, 운영에서 설명이 항상 실패"라고 적었습니다. `CREAM20` 만 토크나이저에 넣어 보고 `SKU-001` 모양은 넣어 보지 않은 채 미루어 적은 것이었습니다. GLOW-127 후속(overlay 회귀) 준비 중에 저장된 PASSED 설명을 보고 확인했습니다.
- **서버 쪽 처리:** 설명 실패 흐름(#28 `explanation_failure`: FAILED·VALIDATING_OUTPUT·시도 3·소진)은 맞게 돕니다. 통과 기대 시험은 `strict xfail` 로 두어, 엔진이 고쳐지면 XPASS 로 실패해 표시를 지우라고 알립니다. 원인을 고정하는 시험(`test_engine_tokenizer_counts_digits_inside_sku_codes`)도 그때 지웁니다.
- **제안(엔진 담당 결정):** 토크나이저가 구분자 없이 영문·숫자가 붙은 코드 토큰(`CREAM20`) 안의 숫자는 수치로 세지 않거나, 코드 토큰으로 인식해 인용 경로 식별자와 맞추게 합니다. mock 템플릿만 바꾸는 것은 실제 모델에 도움이 되지 않습니다.

### 4.2 엔진 저장기는 서버 DB 의 `analysis_runs.run_code`(0006) 를 모른다

- 엔진 v6.2 저장기(`storage.rows_for`)가 만든 실행 행은 `run_code` 가 없어 서버 DB(0007)에서 NOT NULL 로 거부되고, 그 실행을 참조하는 행 8~17개가 FK 로 따라 거부됩니다(처음 실행 43/52).
- 시험에서는 실행 행마다 `run_code`(`AN-260310-9NN`, 서버 `format_run_code`)를 넣어 줍니다. 그 뒤 52/52 통과했고, 0003(`stale_primary_reason`)·0005(`trigger_reason`·`episode_no`)·0007(GRANT) 은 그대로 통과했습니다.
- 정답표의 "저장 시도 전 행이 CHECK·FK 를 모두 통과" 는 v6.2 원본 DDL 기준입니다. 서버 DB 에서는 run_code 가 더 필요하다는 점을 정답표 검토자(GLOW-50)에게 알릴지 사용자 결정입니다.

### 4.3 T11 입력의 `expected_delivery_date` 자리에 엔진 `supply_date` 가 간다

- 계약 7절은 3번째 정렬 키를 "공급일" 이라 하고 엔진 `_rank` 도 `supply_date` 를 쓰는데, 서버 `OptionComparisonInput` 의 필드 이름은 `expected_delivery_date` 입니다(A 구현, GLOW-134). 어댑터는 `supply_date` 를 그 자리에 넣습니다.
- GLOW-50 10건에서는 이 키로 순위가 갈리는 경우가 없어 결과에 영향이 없습니다. 이름을 맞출지는 A·설계 담당 몫입니다.

### 4.4 엔진 fixture 의 JSON v3 검증기 경로가 묶음에 없다

- `fixture_glow50.json_v3_check` 는 `ROOT/review/track_b_policy_analysis/sku_results_v3_validate.py` 를 부르는데 묶음에는 `04_적재검증도구/sku_results_v3_validate.py` 만 있습니다. 시험은 그 파일을 임시 ROOT 아래 그 경로로 복사해 돌립니다(결과 ok, 위반 0).

## 5. 시험 방법과 음성 시험

- **정답표에서 옮김:** 기대값은 정답표 숫자를 그대로 적었고, 엔진 `EXPECTED` 는 참고하지 않았습니다. 정답표에 없는 값(탐침의 순위 등)은 엔진 대조만 합니다.
- **틀린 값을 잡는지:** 기대값 두 개(GS-11 순위, GS-08 H_end)를 일부러 바꾸면 그 두 시험만 실패했습니다. T9 는 KR 재고 0, T11 은 KR NOT_FEASIBLE 로 바꾼 입력이 정답표와 달라지는 것을 봅니다. 저장 검사는 확정 감소를 −1 로 바꾼 행이 CHECK(23514)로 거부되고 그 행을 참조하는 주문별 결과 1행이 FK(23503)로 따라 거부되는 것을 봅니다.
- **DB 를 바꾸지 않음:** 저장 검사 전후 6개 표의 행 수가 같고(`rolled_back` true), 시험용 run_code 가 이미 있으면 건너뜁니다.
- **엔진 묶음 무변경:** `SHA256SUMS.tsv` 118개 대조 — 다른 것은 알려진 Q9 수정 `glowship_engine/options.py` 뿐이고 `tests/test_q9_donor_reason.py` 는 목록에 없는 파일입니다(이전과 같음). 묶음 안에 `output/` 이 생기지 않았습니다.
- **canary 고정:** 설명 시험은 `test_explanation.py` 와 같이 엔진 canary 를 고정합니다(전화번호 패턴 흔들림 방지).

## 6. 남은 일

1. **4.1 엔진 수정 뒤:** `test_evaluation_explanation.py` 의 xfail 표시와 원인 고정 시험·실패 고정 시험을 지우고 9건 모두 통과를 확인합니다.
2. **2단계:** GLOW-125 2부(머지됨)·GLOW-126·T10·GLOW-129 위에서 10건을 DB 입력(`EngineInput`)으로 돌려 T1~T12 전체와 저장 결과를 정답표와 견줍니다. GS-09 는 서버 `StaleJudgmentService`·서명 재계산, GS-10 은 지연 감지·후속 실행 생성까지 잇습니다.
3. **T13·T20 통합 시험**은 가짜 도구 묶음 위에서 따로 끝냈습니다(`docs/agent-flow/integration-tests-t13-t20.md`). 실제 도구 묶음으로 같은 갈래를 돌리는 일은 2단계 뒤입니다.
4. 4.2·4.3 은 사용자가 정답표 검토자·A 에게 알릴지 결정합니다.

## 7. 작업 중 실패와 원인

- **되돌린 파일인데도 실패가 남음:** 기대값을 바꿔 실패를 확인한 뒤 백업으로 되돌렸는데 같은 2건이 또 실패했습니다. 바꾼 글자 수가 같고 같은 초에 써서 `__pycache__` 의 바이트코드가 재사용된 것입니다. `__pycache__` 를 지우니 64건 통과 → 변이 시험 뒤에는 캐시를 지우고 다시 돕니다.
- **엔진 `main()` 출력이 cp949 콘솔에서 깨짐:** `−` 문자 때문에 `UnicodeEncodeError`. 시험은 `redirect_stdout(StringIO())` 로 출력을 가둡니다(탐침은 `sys.stdout.reconfigure(utf-8)`).
- **음성 저장 시험의 기대가 한 행 모자람:** CHECK 위반 행만 거부될 줄 알았는데, 그 행을 참조하는 주문별 결과 1행이 FK 로 따라 거부됐습니다 → 두 행으로 고정했습니다.
- **설명 실패를 처음엔 서버 감싸기 문제로 의심:** 엔진을 직접 불러 같은 실패를 확인하고, 모델 원문의 문장·인용과 토크나이저 결과를 비교해 4.1 원인을 찾았습니다.
- **발견 4.1 범위를 넓게 적음(11-07 바로잡음):** 실제 코드 모양(`SKU-001`)을 토크나이저에 넣어 보지 않고 "숫자가 든 코드 50/50"만 세어 운영에서도 늘 실패한다고 적었습니다 → overlay 의 PASSED 설명 13건과 `CODE` 정규식으로 확인해 fixture 코드 한정으로 고치고, 원인 고정 시험에 `SKU-005` 줄을 더했습니다.
- **엔진 경로 착각:** `GLOWSHIP_ENGINE_DIR` 는 `glowship_engine` 패키지의 부모(`04_적재검증도구`)입니다. 검증기 복사 경로를 처음에 그 위 폴더로 잡아 실패했습니다.
