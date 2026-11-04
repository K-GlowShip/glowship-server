import uuid
from datetime import date

import pytest

from app.modules.data_sync.application.validation.blocking_issues import (
    REQUIRED_DATA_INVALID,
    BlockingIssue,
    OpenIssue,
    find_blocking_issue,
)

"""
    분석 보류 판정 순수 함수 (DB 없음). 조건은 검증 엔진 snapshot.py 의 차단 검증 오류와 같습니다.
"""

EAST = uuid.uuid7()
WEST = uuid.uuid7()
OTHER = uuid.uuid7()
MAPPINGS = {"WMS-EWR01": EAST, "WMS-LAX01": WEST, "WMS-ICN01": OTHER}
BASE_DATE = date(2026, 1, 2)  # 기준일 D → 재고 키 일자 D − 1 = 2026-01-01
ISSUE = OpenIssue("WMS-EWR01|SKU-215|2026-01-01", "NEGATIVE_QUANTITY")


def _find(issues, **change):
    scope = {
        "base_date": BASE_DATE,
        "sku_codes": {"SKU-215", "SKU-216"},
        "warehouse_ids": {EAST, WEST},
    } | change
    return find_blocking_issue(issues, MAPPINGS, **scope)


def test_issue_in_scope_blocks_with_baseline_failure_code():
    """기준선 E9 v1: 도착 창고 WMS-EWR01, 지연 선적 SKU-215, D − 1 재고 이슈 → 보류"""
    blocking = _find([ISSUE])

    assert blocking == BlockingIssue(ISSUE.source_record_key, "NEGATIVE_QUANTITY")
    assert blocking.failure_code == REQUIRED_DATA_INVALID
    assert (
        blocking.failure_message
        == "차단 검증 오류 NEGATIVE_QUANTITY WMS-EWR01|SKU-215|2026-01-01"
    )


@pytest.mark.parametrize(
    ("issue", "change"),
    [
        # 다른 날짜 (D − 1 아님)
        (OpenIssue("WMS-EWR01|SKU-215|2025-12-31", "NEGATIVE_QUANTITY"), {}),
        # 지연 선적 SKU 아님
        (OpenIssue("WMS-EWR01|SKU-999|2026-01-01", "NEGATIVE_QUANTITY"), {}),
        # 매핑되지 않은 창고 (기준선 WMS-ATL01, UNKNOWN_WAREHOUSE)
        (OpenIssue("WMS-ATL01|SKU-215|2026-01-01", "UNKNOWN_WAREHOUSE"), {}),
        # 매핑은 됐지만 분석 창고(도착·공여)가 아님
        (OpenIssue("WMS-ICN01|SKU-215|2026-01-01", "NEGATIVE_QUANTITY"), {}),
        # 재고 키 모양이 아님 (주문·선적 키)
        (OpenIssue("B-ORD-ERR-0001", "UNKNOWN_SKU"), {}),
        # 같은 이슈라도 분석 창고에 도착 창고가 없으면
        (ISSUE, {"warehouse_ids": {WEST}}),
    ],
)
def test_issue_out_of_scope_does_not_block(issue, change):
    assert _find([issue], **change) is None


def test_first_blocking_issue_in_given_order_is_returned():
    """여러 개면 조회 순서(감지 시각 순)에서 처음 걸리는 것 하나"""
    later = OpenIssue("WMS-LAX01|SKU-216|2026-01-01", "NEGATIVE_QUANTITY")
    unrelated = OpenIssue("WMS-ATL01|SKU-215|2026-01-01", "UNKNOWN_WAREHOUSE")

    assert _find([unrelated, ISSUE, later]).source_record_key == ISSUE.source_record_key


def test_no_issues_do_not_block():
    assert _find([]) is None
