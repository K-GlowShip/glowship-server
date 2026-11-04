import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Protocol

"""
    분석을 막는 검증 이슈 판정 (GLOW-15)
      - 분석 입력을 확정할 때(실행 생성) 부릅니다. 걸리면 그 실행은 ON_HOLD 입니다
        (failed_step = VALIDATING, failure_code = REQUIRED_DATA_INVALID, 기준선 E9 v1).
      - 조건은 검증 엔진 snapshot.py 의 "차단 검증 오류"와 같습니다.
          1) 확정 시각까지 감지되었고(detected_at ≤ 확정) 그때 아직 해소되지 않은(resolved_at 없음 또는 > 확정)
             분석 차단(blocks_analysis) 이슈
          2) 재고(INVENTORY) 이슈이고, 키 `창고|SKU|일자`의 일자 = 기준일 D − 1
          3) SKU 자리가 지연 선적 SKU 의 내부 sku_code 중 하나
          4) 창고 자리(원천 외부 코드)가 확정 시각까지 MAPPED 된 재고 창고 매핑으로 분석 창고(도착·공여) 중 하나.
             매핑되지 않은 창고 코드(UNKNOWN_WAREHOUSE)는 분석 창고의 행이 아니라 막지 않습니다.
      - 주문·선적 이슈는 막지 않습니다 (엔진과 같음).
      - 판정은 DB 를 읽지 않는 순수 함수(find_blocking_issue)이고, 조회는 Repository 가 합니다.
"""

# 보류된 실행의 failure_code (기준선 E9 v1)
REQUIRED_DATA_INVALID = "REQUIRED_DATA_INVALID"


@dataclass(frozen=True)
class OpenIssue:
    """확정 시각에 열려 있던 분석 차단 재고 이슈"""

    source_record_key: str
    error_code: str


@dataclass(frozen=True)
class BlockingIssue:
    """분석을 막은 이슈. 실행의 실패 요약(failure_code·failure_message)에 씁니다."""

    source_record_key: str
    error_code: str
    failure_code: str = REQUIRED_DATA_INVALID

    @property
    def failure_message(self) -> str:
        return f"차단 검증 오류 {self.error_code} {self.source_record_key}"


class BlockingIssueRepository(Protocol):
    """BlockingIssueService 가 쓰는 조회 (구현: infrastructure 의 ValidationIssueRepository)"""

    async def find_open_inventory_issues(
        self, company_id: uuid.UUID, at: datetime
    ) -> Sequence[OpenIssue]: ...

    async def inventory_warehouse_mappings(
        self, company_id: uuid.UUID, at: datetime
    ) -> Mapping[str, uuid.UUID]: ...


def find_blocking_issue(
    issues: Sequence[OpenIssue],
    warehouse_of_external: Mapping[str, uuid.UUID],
    *,
    base_date: date,
    sku_codes: Collection[str],
    warehouse_ids: Collection[uuid.UUID],
) -> BlockingIssue | None:
    """issues 순서(감지 시각 순)에서 처음 걸리는 이슈 하나. 없으면 None."""
    day = (base_date - timedelta(days=1)).isoformat()
    for issue in issues:
        parts = issue.source_record_key.split("|")
        if len(parts) != 3:
            continue
        warehouse_code, sku_code, issue_day = parts
        if (
            issue_day == day
            and sku_code in sku_codes
            and warehouse_of_external.get(warehouse_code) in warehouse_ids
        ):
            return BlockingIssue(issue.source_record_key, issue.error_code)
    return None


class BlockingIssueService:
    def __init__(self, repo: BlockingIssueRepository):
        self.repo = repo

    async def find(
        self,
        company_id: uuid.UUID,
        *,
        confirmed_at: datetime,
        base_date: date,
        sku_codes: Collection[str],
        warehouse_ids: Collection[uuid.UUID],
    ) -> BlockingIssue | None:
        """
        분석 입력 확정 시각(confirmed_at) 기준으로 분석을 막는 이슈를 찾습니다.
        base_date 는 분석 기준일 D, sku_codes 는 지연 선적 SKU 의 내부 코드, warehouse_ids 는 도착·공여 창고입니다.
        """
        issues = await self.repo.find_open_inventory_issues(company_id, confirmed_at)
        if not issues:
            return None
        mappings = await self.repo.inventory_warehouse_mappings(
            company_id, confirmed_at
        )
        return find_blocking_issue(
            issues,
            mappings,
            base_date=base_date,
            sku_codes=sku_codes,
            warehouse_ids=warehouse_ids,
        )
