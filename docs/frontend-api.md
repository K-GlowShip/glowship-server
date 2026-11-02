# 프론트엔드에서 사용 가능한 API

개발이 끝나 프론트엔드에서 바로 호출해도 되는 API만 적습니다. 경로는 `/api/v1` 뒤에 붙습니다.

| 메서드 | 경로 | 설명 | 티켓 | 추가일 |
| --- | --- | --- | --- | --- |
| GET | `/delay-events` | 지연 목록·탭별 건수. 필터 `status`(ALL·PENDING_DECISION·ANALYZING·ON_HOLD·NEEDS_REANALYSIS), `warehouseId`, `from`·`to`(UTC 날짜, to 포함), `q`, `hasFinalDecision`. 페이지 `page`(기본 1)·`size`(기본 20, 최대 100), 응답 `page`·`size`·`totalCount`·`totalPages`(모든 필터 적용)와 `counts`(탭별, 탭·hasFinalDecision 필터 제외). `runCode`는 아직 null | GLOW-16 | 11/02 |

목록 API 공통: 요청 `page`·`size`, 응답 `items`·`page`·`size`·`totalCount`·`totalPages` (`app/core/pagination.py`)
| GET | `/delay-events/{delayEventId}` | 지연 상세·분석 실행 버전 이력. 다른 고객사 건은 404 | GLOW-16 | 11/02 |
