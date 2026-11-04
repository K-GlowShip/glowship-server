# 데이터 팀 분석 엔진 불러오기 (로컬 경로 방식)

- 대상: `app/modules/delay_analysis/infrastructure/engine/engine_loader.py`, `engine_manifest.json`, 설정 `GLOWSHIP_ENGINE_DIR`
- 쓰는 곳(다음 작업): 계산 연결(입력 스냅샷·재고 영향·위험 주문·입력 검증), AI 설명 생성·검증, STALE 판정 서명 재계산

## 1. 왜 저장소에 넣지 않나
- 엔진 `glowship_engine`과 주문 배정 모듈 `allocation_core`는 데이터 팀 결과물입니다. 원본은 공유 묶음과 **비공개** 저장소 glowship-plan에 있습니다.
- 이 서버 저장소는 **공개** 저장소라, 코드를 넣으면 엔진·설명 프롬프트·금지 표현 사전이 공개됩니다.
- 그래서 저장소에는 **파일 목록과 SHA-256(`engine_manifest.json`, 41개)만** 넣습니다. 실행할 때 각자 PC의 원본을 이 목록으로 대조해 불러옵니다. 판이 어긋난 엔진으로는 계산하지 않습니다.

## 2. 처음 한 번 준비하기
1. 공유 묶음 `00_필수_최신DB_전체재적재\04_적재검증도구\`에 `glowship_engine\`이 있는지 확인합니다.
2. glowship-plan 저장소를 최신으로 받고(`git pull`), `tools\synth2025_v5\` 폴더를 통째로 `04_적재검증도구\synth2025_v5\`로 복사합니다. glowship-plan README "묶음에서 빠진 엔진 의존 파일"과 같은 절차입니다.
   ```
   04_적재검증도구\
   ├── glowship_engine\      (공유 묶음)
   └── synth2025_v5\         (glowship-plan tools 에서 복사)
       ├── allocation_core.py
       └── tests\
   ```
3. `.env`에 경로를 넣습니다(`.env.example` 끝 참고).
   ```
   GLOWSHIP_ENGINE_DIR=C:/…/00_필수_최신DB_전체재적재/04_적재검증도구
   ```
4. 확인합니다. 아래가 건너뜀 없이 10건 통과해야 합니다.
   ```
   uv run pytest tests/modules/delay_analysis/test_engine_loader.py -rs
   ```

## 3. 불러오는 방식
| 단계 | 내용 |
| --- | --- |
| 경로 확인 | `GLOWSHIP_ENGINE_DIR`이 비었거나 그 아래에 `glowship_engine`이 없으면 `EngineNotConfiguredError` |
| 해시 대조 | 목록의 41개가 모두 있고 바이트가 같은지 봅니다. 하나라도 없거나 다르면 `EngineIntegrityError`(없는 파일·다른 파일 목록 포함). 목록에 없는 파일(하네스·배포 도구 등)은 보지 않습니다 |
| 불러오기 | 경로를 `sys.path`에 넣고 `glowship_engine.<모듈>`을 import 합니다. 엔진 `core.py`·`snapshot.py`가 형제 폴더 `synth2025_v5`를 직접 찾으므로 두 폴더는 나란히 있어야 합니다 |
| 캐시 파일 | 불러올 때 원본 폴더에 `.pyc`를 만들지 않습니다(`sys.dont_write_bytecode`). 공유 묶음을 바꾸지 않기 위해서입니다 |
| 한 번만 | 해시 대조는 프로세스당 한 번 합니다(`load_verified_engine_dir`) |

```python
from app.modules.delay_analysis.infrastructure.engine.engine_loader import (
    load_engine_module,
)

pipeline = load_engine_module("pipeline")
validator = load_engine_module("explain.validator")
```

## 4. 엔진 새 판을 받을 때
1. 데이터 팀이 새 이름·새 해시로 낸 판만 받습니다.
2. 받은 파일로 `engine_manifest.json`을 다시 만들고, 출처 기록(overlay manifest 또는 glowship-plan README)과 대조합니다.
3. PR로 목록을 바꿉니다. 팀원은 자기 PC의 원본도 같은 판으로 바꿔야 합니다. 바꾸지 않으면 `EngineIntegrityError`로 바로 알 수 있습니다.

## 5. 시험
| 시험 | 엔진 없이 | 엔진 있을 때 |
| --- | --- | --- |
| 목록 41개·필수 파일 | 돎 | 돎 |
| 해시 대조(맞음 / 줄바꿈만 바뀜 / 파일 없음), 경로 확인, `.pyc` 안 만듦 | 돎(임시 폴더) | 돎 |
| 실제 원본이 목록과 같음 | **건너뜀**(사유 표시) | 돎 |
| 엔진 자체 시험 3개(50·97·57건) | **건너뜀** | 돎 |
| 배정 모듈 판정 실행기(`run_g2.py`, G2) | **건너뜀** | 돎 |

- CI(GitHub Actions)는 지금 ruff만 돌고, 엔진 원본이 없으므로 엔진 시험은 로컬에서 확인합니다.
- 경로를 설정하지 않은 PC에서는 엔진 시험이 건너뛰어집니다. `pytest -rs`로 건너뜀 사유를 볼 수 있습니다.

## 6. 배포
- 배포 서버에도 같은 폴더 모양으로 원본을 두고 `GLOWSHIP_ENGINE_DIR`을 설정해야 합니다.
- 이미지나 서버에 원본을 넣는 단계는 배포 작업(인프라)에서 정합니다.
- 원본이 없거나 다르면 엔진을 쓰는 기능이 시작 단계에서 오류를 냅니다.

## 7. 작업 중 실패와 결정
- **처음 방식:** 엔진 원본 41개를 저장소 `vendor/tools/`에 그대로 복사하는 방식으로 만들었습니다(로컬 브랜치, push 안 함). 그 뒤 이 저장소가 공개라는 점을 확인하고, 데이터 팀 허락이 필요 없는 지금 방식으로 바꿨습니다.
- **줄바꿈 판단:** 처음에는 엔진 설명 자산이 CRLF라고 잘못 봤습니다. 셸에서 CR을 세는 명령이 글자 `r`을 셌기 때문입니다. 실제로는 모두 LF입니다. 해시 대조는 바이트 그대로 하므로 줄바꿈이 바뀌면 잡힙니다(시험으로 확인).
- **`.pyc` 파일:** 공유 묶음 안에 `__pycache__`(`.pyc` 4개, 11-04 새벽 시각)가 있습니다. 이전 작업에서 엔진을 묶음 경로에서 직접 import 할 때 생긴 것으로 보입니다. 지금 불러오기 코드는 `.pyc`를 만들지 않습니다. 남은 4개는 지워도 되는 캐시입니다.
