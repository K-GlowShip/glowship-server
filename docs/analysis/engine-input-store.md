# 엔진 입력 저장소 (한 고객사, 서버 DB에서 읽기)

- 대상: `app/modules/delay_analysis/infrastructure/engine/engine_input_store.py`, 시험 `tests/modules/delay_analysis/test_engine_input_store.py`
- 티켓: GLOW-125 (FR-008 관련 자료 조회. 인수 조건 "해당 고객사의 자료만 조회되고, 사용한 데이터와 기준 버전이 실행에 기록된다")
- 쓰는 곳(다음 작업): 조회 도구 T1~T5·T8, 재고 영향·위험 주문 T6·T7(엔진 `snapshot.build` → `pipeline.analyze`)

## 1. 왜 필요한가
- 엔진의 입력 스냅샷 `snapshot.build(store, …)`는 업무 DB를 메모리에 올린 `Store`를 받습니다.
- 엔진 `Store`는 `docker exec <컨테이너> psql -U postgres`로 **DB 전체(모든 고객사)**를 읽습니다(`store.psql_copy`). 서버에서는 쓸 수 없습니다.
  - 컨테이너·계정이 정해져 있습니다.
  - 고객사 범위가 없습니다(BR-001).
- 엔진 원본은 고치지 않기로 했습니다(`engine-local-path.md`). 그래서 **읽는 부분만** 서버 연결로 바꾸고, as-of 조회와 스냅샷은 엔진 것을 그대로 씁니다.

## 2. 방식
| 단계 | 내용 |
| --- | --- |
| 고객사 범위 | RLS 컨텍스트 `app.company_id`를 설정하고, 조회 조건에도 `company_id = $1`을 넣습니다. RLS가 우회되는 로컬 superuser 접속에서도 다른 고객사 행은 읽히지 않습니다(`run_status_repository`와 같은 방식) |
| 읽는 표 | 엔진 `store.TABLES` 14개 + 변경 기록(엔진 `DCR_SQL` 범위). 서버 쪽 허용 목록 `ENGINE_TABLES`와 엔진 목록이 다르면 멈춥니다(엔진 새 판 감지, SQL에 넣는 표 이름 제한) |
| 읽는 방법 | 엔진과 같은 `COPY (…) TO STDOUT CSV HEADER`를 서버 연결(asyncpg `copy_from_query`)로 실행합니다. 시각·숫자·불리언·JSON의 글자 모양이 엔진이 psql로 받는 것과 같고, 엔진 변환 함수 `iso_ts`·`bool_text`를 그대로 씁니다 |
| 행 순서 | `ORDER BY ctid`. 엔진은 조건 없이 전체를 읽어 물리 순서로 받습니다. 고객사 조건을 걸면 색인 순서로 올 수 있어 순서를 맞춥니다(3절 실패 기록) |
| Store 만들기 | 엔진 `Store`를 상속하고 `__init__`(색인 만들기)만 바꿉니다. 색인 식은 엔진 `Store.__init__`과 같은 순서·같은 식이고, as-of 메서드(`runs_until`·`input_seq`·`snapshot_at`·`inventory_row` 등)는 엔진 것을 그대로 씁니다 |
| 입력 순번 | 엔진은 "수집 시작 시각 ≤ 입력 확정 시각"으로 그때 알려진 자료를 고릅니다. `check_input_seq`가 이 기준의 순번이 실행의 `input_seq`와 같은지 확인하고, 다르면 `EngineInputError`로 멈춥니다. 읽을 때 순번으로 거르지 않는 이유는 엔진 결과와 갈라지지 않게 하려는 것입니다 |
| 시각 | `engine_timestamp`: 서버 시각 → UTC `YYYY-MM-DDTHH:MM:SSZ`. 엔진은 1초 미만을 다루지 않으므로 초 아래는 버립니다. 시간대 없는 시각은 거부합니다 |

```python
store = await load_company_engine_store(session, company_id)  # 워커 세션, 읽기 전용
confirm_t = engine_timestamp(run.started_at)
check_input_seq(store, company_id, confirm_t, run.input_seq)
snap = load_engine_module("snapshot").build(
    store, calendar, shipment_id, D, confirm_t, stage=..., E=...
)
```

## 3. 시험
| 시험 | 조건 | 내용 |
| --- | --- | --- |
| 단위 5건 | 엔진·DB 없이 | CSV 해석(시각·불리언 열만 변환), 빈 결과, 시각 변환(UTC·초 아래 버림·시간대 없음 거부), 입력 순번 불일치 → 오류, 엔진 표 목록이 다르면 멈춤 |
| DB 2건 | `GLOWSHIP_ENGINE_DIR` + overlay DB | B_COSMETICS만 읽어 표마다 행 수 = `count(*)`, 모든 행의 고객사 = B. 실행 8건 모두 입력 순번이 같음, NO_RISK 실행 스냅샷이 만들어지고 `input_seq`가 같음 |
| 정답 대조 2건 | 위 + `GLOWSHIP_ENGINE_GOLDEN=1` + docker | 엔진 원래 방식(psql `COPY`로 전체 읽기 → 고객사 행만)으로 엔진 `Store.__init__`을 그대로 돌린 Store와 **속성 22개가 행 순서까지 같은지**, overlay 실행(A 13건·B 8건)의 `snapshot.build` 결과(보류 코드 포함)가 같은지. 값 하나를 바꾸면 다르다고 나오는지(음성). 약 70초 |

```
GLOWSHIP_ENGINE_GOLDEN=1 uv run pytest tests/modules/delay_analysis/test_engine_input_store.py -rs
```
- 정답 대조는 컨테이너 이름이 `glowship-db`라고 봅니다. 다르면 `GLOWSHIP_DB_CONTAINER`로 바꿉니다. 접속 계정·DB는 `.env` 값을 씁니다.
- **11-05 결과(개발 1 DB `glowship-db`, overlay 21건):**
  - 두 고객사 모두 속성 22개가 같습니다.
  - 스냅샷 21건이 같습니다(19건 스냅샷, 2건 `REQUIRED_DATA_INVALID` 보류).
  - 입력 순번 21건이 모두 실행 기록과 같습니다.
- **음성 시험:** `ORDER BY ctid`를 빼면 B_COSMETICS의 `t`(skus 순서)가 달라 정답 대조가 실패합니다.

## 4. 측정과 확인이 필요한 것
- **읽는 시간:** A_BEAUTY 한 고객사에 약 9.5초가 걸립니다.
  - 표 14개는 주문 10.7만·주문 상세 16만·재고 3.6만 행이고, 변경 기록은 26.6만 행입니다.
  - 대부분 Python의 CSV 해석과 시각 변환입니다.
  - 엔진 원래 방식은 두 고객사 전체에 약 21초입니다.
  - 감지 후 30초 안 결과(NFR-001)를 생각하면 크므로, 실행기 연결 때 정합니다.
  - 후보는 두 가지입니다: 같은 고객사·같은 입력 순번이면 워커 프로세스 안에서 다시 쓰기, 변경 기록을 엔진이 쓰는 종류(ORDER_LINE UPDATE 등)로 줄이기. 줄이는 쪽은 엔진 결과와 같은지 정답 대조로 다시 확인해야 합니다.
- **1초 미만 시각:** 엔진 `iso_ts`는 1초 미만 값을 거부합니다. overlay 자료는 모두 초 단위라 지금은 문제가 없습니다. 서버가 실시간으로 쓰는 수집(`data_sync_runs.started_at` 등)이 마이크로초를 가지면 읽을 때 `ValueError`가 납니다. 서버 수집의 시각 기록 방식과 함께 정해야 합니다(데이터 팀·B).
- **실행 시작 시각:** `create_analysis_run`은 `clock_timestamp()`(마이크로초 포함)를 씁니다. 엔진 입력에는 초 아래를 버린 시각을 넘기고, 그래서 생길 수 있는 어긋남은 `check_input_seq`가 잡습니다.
- **엔진 상수:** 엔진 `DCR_SQL`은 `started_at >= '2026-01-01'`로 고정돼 있습니다. 엔진 판의 결정이라 서버는 그대로 따릅니다.

## 5. 작업 중 실패와 결정
- **행 순서(11-05):** 처음에는 고객사 조건만 걸어 읽었습니다. 정답 대조에서 B_COSMETICS의 `skus` 순서가 엔진과 달랐습니다. 내용은 같고 순서만 달랐습니다. 고객사 조건 때문에 DB가 색인 순서로 돌려준 것입니다. 엔진은 순서에 기대는 곳이 있으므로(창고·SKU 반복 순서) `ORDER BY ctid`로 물리 순서를 맞췄습니다.
- **탐색 스크립트의 가짜 오류(11-05):** 스냅샷 비교를 JSON으로 하다가, 스냅샷의 튜플 키 때문에 `TypeError`가 났습니다. 이것을 `snapshot.build` 실패로 잘못 읽을 뻔했습니다. 시험은 `==`로 비교합니다.
- **엔진 경로(11-05):** 처음에 `synth2025_v5`와 Q9 수정판이 없는 묶음 경로로 돌려 실제 엔진 시험이 실패했습니다. 준비된 묶음(`engine-local-path.md` 2절)으로 돌려야 합니다.
- **읽을 때 순번으로 거르지 않음:** "입력 순번 이하 수집분만 읽기"도 생각했습니다. 하지만 엔진은 시각 기준이라, 커밋 순서와 시작 순서가 다른 수집이 있으면 엔진 결과와 갈라집니다. 엔진 판단은 그대로 두고, 같은지 확인하는 쪽(`check_input_seq`)을 골랐습니다.
- **색인 코드를 옮겨 적음:** 엔진 `Store.__init__`은 읽기와 색인 만들기가 한 함수라 일부만 다시 쓸 수 없습니다. 같은 식을 옮겨 적고, 정답 대조 시험이 엔진이 직접 만든 Store와 속성을 하나씩 비교합니다. 엔진 새 판(manifest 변경) 때 이 시험을 꼭 돌립니다.
