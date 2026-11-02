# 프론트엔드에서 사용 가능한 API

개발이 끝나 프론트엔드에서 바로 호출해도 되는 API만 적습니다. 경로는 `/api/v1` 뒤에 붙습니다.

| 메서드 | 경로 | 설명 | 티켓 | 추가일 |
| --- | --- | --- | --- | --- |
| GET | `/data-sync-runs/latest` | 원천 유형(ORDER·INVENTORY·SHIPMENT·PRODUCT·WAREHOUSE)별 마지막 수집 실행·미해결 이슈 수·원천 시스템 이름·수집 주기와 서버 판정 `isSynced`·`lastSyncedAt` (명세 v6.2 4.3). 고객사 사용자 전용. [문서](data-sync/latest-sync-status.md) | GLOW-97 | 2026-11-02 |
