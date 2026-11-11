# STALE 판정 서명 재계산 (이슈 #75)

- 대상: `app/modules/delay_analysis/infrastructure/engine/signature_recalculator.py`(`EngineSignatureRecalculator`, `recalculator_or_none`), 연결 `manual_sync_service._ingest`·`run_start/outcome_save.save_graph_outcome`
- 근거: D-13 의존 입력 기반 STALE, 정책 결정 ST-3 C안(1차 의존 범위 → 2차 판정 서명 재계산), 엔진 `glowship_engine/stale.py` `judge`

## 1. 왜

STALE 판정은 두 단계입니다(`stale_service.py`).
- 1차: 수집의 변경 기록이 실행의 의존 범위에 걸리는가.
- 2차: 걸리면 판정 서명을 다시 계산해 **바뀌었을 때만** STALE.

2차는 `SignatureRecalculator`로 주입받게 만들어 두었지만 부르는 쪽이 없어, 지금까지는 1차에 걸리면 모두 STALE(놓치지 않는 쪽)이었습니다. 그래서 결과가 같은 실행까지 STALE이 되어 "다시 분석하세요"가 필요 이상으로 나왔고, 확정 데이터(엔진 정답)와 STALE 시점이 달랐습니다(평가 시나리오 GS-09, `evaluation-scenarios.md`).

## 2. 계산 방법 (엔진 `stale.judge`와 같음)

| 값 | 정하는 법 |
| --- | --- |
| 기준일 D | 실행의 입력 순번 저장소에서 `resolve_base_date`(분석 때와 같음, 실행에 저장돼 있지 않음) |
| 입력 확정 시각·단계·예상 입고일 | 실행 그대로(`_run_input`, `build_engine_input`과 같음) |
| 새 입력 | 이번 변경의 마지막 적용 순번까지의 저장소 |
| 알려진 시각(`knowledge_t`) | 그 수집의 완료 시각 |

`snapshot.build(…, knowledge_t)` → `pipeline.analyze` → 해시가 실행의 `decision_signature_sha256`과 다르면 STALE. 수집 순서대로 보고 처음 바뀐 수집에서 STALE로 기록합니다(`stale_service._judge`, 앞선 수집까지 누적).

**다르다고 보는 경우(놓치지 않는 쪽):** 실행에 서명이 없음, 수집 완료 시각이 없음, 재계산 입력이 엔진 Hold 또는 계산이 지원하지 않는 운영 기준, 엔진 쪽 오류(경고 로그). DB 오류는 트랜잭션이 깨졌으므로 그대로 올립니다.

## 3. 어디서 쓰나

| 곳 | 내용 |
| --- | --- |
| 수동 동기화(`manual_sync_service._ingest`) | 수집 적재 트랜잭션의 STALE 판정(`judge_after_sync`) |
| 결과 저장 뒤 재판정(`save_graph_outcome` → `save_analysis_result`) | 입력 순번 이후 변경 전체 재판정(`recheck_run`, Q-13a) |
| 엔진 경로가 없을 때 | `recalculator_or_none`이 `None` → 1차만(지금까지와 같음). CI·엔진 없이 띄운 서버 |
| 데모 시드(`scripts/seed_demo_runs.py`) | 바꾸지 않음(시드 수집 때 데모 선적 실행이 아직 없어 결과 같음) |

진행 중 실행(`DETECTED`~`VALIDATING_OUTPUT`)은 서명이 없으므로 지금처럼 1차 결과로 판정합니다(`stale_service._signature_changed`).

## 4. 저장소 읽기와 캐시

- 실행의 입력 순번 저장소는 이미 커밋된 순번이라 캐시(#60, `get_company_engine_store`)를 씁니다.
- 새 입력 순번 저장소는 **캐시에 넣지 않습니다.** 수집 트랜잭션 안(커밋 전)에서 읽으므로, 되돌려지면 같은 순번이 다른 내용으로 다시 쓰일 수 있습니다. 재계산기 하나(트랜잭션 하나) 안에서는 **마지막으로 읽은 순번 하나만** 들고 있다가 같은 순번이면 다시 씁니다. 저장소 하나가 로컬에서 약 0.45GB(PR #116 리뷰, 저장소 3개를 든 시험의 최대 RSS 약 1.5GB)라 순번마다 쌓지 않습니다. `_judge`는 순번을 오름차순으로 보므로 한 실행 안에서 지난 순번으로 돌아가지 않습니다.

### 걸리는 시간 (로컬, PR #116 리뷰 측정)

거의 전부가 저장소 읽기(`load_company_engine_store`)이고, 엔진 계산은 짧습니다.

| 경우 | 실행 입력 순번 저장소(캐시됨) | 새 순번 저장소(캐시 안 됨) | 엔진 계산 |
| --- | --- | --- | --- |
| B AN-260103-002, 순번 23 | 7.4초 | 8.2초 | 0.2초 |
| A AN-260202-001, 순번 173 | 11.6초 | 13.7초 | 0.2초 |

- **수집 트랜잭션(L1)이 길어짐:** 1차에 걸린 실행이 있는 수집마다 새 순번 저장소를 L1 안에서 한 번 읽습니다(`judge_after_sync` 한 번에 7~14초).
- **데모 동기화 버튼 한 번:** 데모 DB 임시 복사본에서 API 확인 전체 46초 안에서 끝남. 버튼은 202 를 먼저 돌려주고 수집은 뒤에서 돕니다.
- **"다음 날로"(144 tick, 데모 DB 임시 복사본, 11-11):** 재계산기 없이 52초, 재계산기를 켜고 **241초**. 끝난 뒤 상태는 재계산기 없이 SEA·PORT·PRE 모두 `STALE`, 켜면 PORT 만 `STALE`(SEA `READY_FOR_REVIEW`, PRE `NO_RISK` 그대로). 시연은 "다음 날로"를 누르지 않습니다.
- **결과 저장 뒤 재판정(`recheck_run`):** 입력 순번 뒤에 1차에 걸리는 수집이 많으면 오래 걸립니다. 확정 데이터 A AN-260115-001 은 걸리는 수집 13번에 247초였고, 그동안 그 실행의 L2 를 잡고 있습니다. 그사이 같은 실행을 판정하려는 수동 동기화는 L1 을 잡은 채 L2 를 기다릴 수 있습니다. 보통은 분석이 짧아 그 사이 수집이 0~1번이라 문제가 되지 않습니다.

## 5. 확인

| 확인 | 결과 |
| --- | --- |
| 확정 데이터 STALE 실행 11건(사유 `RELEVANT_*`) 정답 대조 | **11건 모두 저장된 `stale_detected_seq`와 같음.** 그중 4건(A AN-260115-001·002, AN-260202-001, B AN-260103-002)은 1차에 먼저 걸리는 수집이 있었지만 서명이 같아 넘어갔고, 정답 순번에서 STALE(재계산기 없이는 그 앞 순번에서 STALE) |
| 시험 `test_signature_recalculator_sql.py` | B AN-260103-002: 재계산기 없이 23 에서 STALE, 재계산기로 27 까지 그대로·28 에서 STALE(정답). 엔진·확정 데이터가 없으면 건너뜀 |
| 시험 `test_signature_recalculator.py` | 분기(서명 없음·변경 없음·완료 시각 없음·같음·다름·보류·엔진 오류·DB 오류·엔진 경로 없음), 순번별 한 번 읽기 |
| 변이 | 재계산 결과를 늘 "다름"으로 바꾸면 SQL 시험 2건이 실패 |
| 데모(`glowship_demo` 임시 복사본) | 동기화 한 번 뒤 항구 `STALE`(주문 수집에서 서명이 바뀜)·선택 409, 출항 전은 서명이 같아 `NO_RISK` 그대로 |
