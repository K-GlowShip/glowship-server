import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ARRAY, UUID, Integer, Text, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

"""
    판단 이력 조회 (API 명세 v6.2 4.7 GET /decisions, 의사결정 이력 화면 SCR-004 의 목록)
      - 팀원 초안(member_drafts)과 팀장 최종 선택(final_decisions)을 같은 열 모양으로 세워(UNION ALL) 한 목록으로 만든다.
        초안의 decided_at 은 작성 시각(created_at), 최종 선택은 선택 시각(decided_at)이다 (명세 4.7).
      - 행마다 분석 실행(run 요약·status 계산 근거) → 지연 이벤트 → 선적, 판단한 사용자, 고른 대응안(초안은 없을 수 있음)을 붙인다.
      - status 는 저장값이 아니라 계산값이다: FINAL / 초안이고 실행이 STALE 이면 NEEDS_REANALYSIS / 그 밖의 초안은 DRAFT (명세 4.7 상태 표).
        필터 status 와 건수가 같은 식을 보도록 CTE 안에서 계산한다.
      - 필터: 기간(decided_at, [from, to)), 유형, 상태, 선택자, 실행, 검색어. 값이 없으면(NULL) 그 조건은 통과한다.
        검색어는 지연 목록(4.4)과 같은 범위다: 선적 코드, 또는 그 선적에 실린 SKU 코드·상품명 부분 일치(대소문자 무시).
      - 정렬은 decided_at 최신순, 같으면 decision_id (명세 4.7, 응답 본문 규칙 6.2). 두 표의 ID 는 서로 겹치지 않는다.
      - 대표 상품(D13)은 페이지의 선적 ID 배열로 상세 줄을 한 번에 읽어 서비스가 고른다 (목록 1회 + 건수 1회 + 상세 1회).
      - 운영에서는 RLS 가 고객사 범위를 강제하지만, 로컬 개발 DB 는 슈퍼유저라 RLS 를 우회하므로 모든 조인에 company_id 를 명시한다 (ERD 설명 4.6).
        다른 고객사의 사용자·실행 ID 를 필터로 받아도 조인 조건 때문에 0건이다.
"""

DECISION_TYPE_DRAFT = "DRAFT"
DECISION_TYPE_FINAL = "FINAL"
STATUS_DRAFT = "DRAFT"
STATUS_FINAL = "FINAL"
STATUS_NEEDS_REANALYSIS = "NEEDS_REANALYSIS"

_LIKE_ESCAPE = "\\"
_UUID = UUID(as_uuid=True)

_FILTERED = f"""
    WITH decisions AS (
        SELECT '{DECISION_TYPE_DRAFT}' AS decision_type, d.member_draft_id AS decision_id,
               d.created_at AS decided_at, d.company_id, d.analysis_run_id, d.user_id, d.response_option_id
          FROM member_drafts d
         WHERE d.company_id = :company_id
        UNION ALL
        SELECT '{DECISION_TYPE_FINAL}', f.final_decision_id, f.decided_at,
               f.company_id, f.analysis_run_id, f.user_id, f.response_option_id
          FROM final_decisions f
         WHERE f.company_id = :company_id
    ),
    joined AS (
        SELECT x.decision_type, x.decision_id, x.decided_at,
               CASE WHEN x.decision_type = '{DECISION_TYPE_FINAL}' THEN '{STATUS_FINAL}'
                    WHEN r.status = 'STALE' THEN '{STATUS_NEEDS_REANALYSIS}'
                    ELSE '{STATUS_DRAFT}' END AS status,
               r.analysis_run_id AS run_id, r.run_code, r.status AS run_status,
               r.result_class AS run_result_class, r.failure_code AS run_failure_code,
               r.delay_event_id,
               s.shipment_id, s.shipment_code,
               u.user_id, u.display_name, u.role_code,
               o.response_option_id, o.option_type
          FROM decisions x
          JOIN analysis_runs r
            ON r.company_id = x.company_id AND r.analysis_run_id = x.analysis_run_id
          JOIN delay_events e
            ON e.company_id = r.company_id AND e.delay_event_id = r.delay_event_id
          JOIN shipments s
            ON s.company_id = e.company_id AND s.shipment_id = e.shipment_id
          JOIN users u
            ON u.company_id = x.company_id AND u.user_id = x.user_id
          LEFT JOIN response_options o
            ON o.company_id = x.company_id AND o.analysis_run_id = x.analysis_run_id
           AND o.response_option_id = x.response_option_id
         WHERE (CAST(:decided_from AS timestamptz) IS NULL OR x.decided_at >= :decided_from)
           AND (CAST(:decided_to AS timestamptz) IS NULL OR x.decided_at < :decided_to)
           AND (CAST(:decision_type AS text) IS NULL OR x.decision_type = :decision_type)
           AND (CAST(:decided_by AS uuid) IS NULL OR x.user_id = :decided_by)
           AND (CAST(:run_id AS uuid) IS NULL OR x.analysis_run_id = :run_id)
           AND (CAST(:pattern AS text) IS NULL
                OR s.shipment_code ILIKE :pattern ESCAPE '\\'
                OR EXISTS (SELECT 1
                             FROM shipment_lines l
                             JOIN skus k ON k.company_id = l.company_id AND k.sku_id = l.sku_id
                            WHERE l.company_id = s.company_id AND l.shipment_id = s.shipment_id
                              AND (k.sku_code ILIKE :pattern ESCAPE '\\'
                                   OR k.product_name ILIKE :pattern ESCAPE '\\')))
    ),
    filtered AS (
        SELECT * FROM joined
         WHERE (CAST(:status AS text) IS NULL OR status = :status)
    )
"""

_FILTER_PARAMS = (
    bindparam("company_id", type_=_UUID),
    bindparam("decided_from"),
    bindparam("decided_to"),
    bindparam("decision_type", type_=Text()),
    bindparam("status", type_=Text()),
    bindparam("decided_by", type_=_UUID),
    bindparam("run_id", type_=_UUID),
    bindparam("pattern", type_=Text()),
)

_LIST_PAGE = text(
    _FILTERED
    + """
    SELECT decision_type, decision_id, decided_at, status,
           run_id, run_code, run_status, run_result_class, run_failure_code, delay_event_id,
           shipment_id, shipment_code,
           user_id, display_name, role_code,
           response_option_id, option_type
      FROM filtered
     ORDER BY decided_at DESC, decision_id
    OFFSET :offset LIMIT :limit
    """
).bindparams(
    *_FILTER_PARAMS,
    bindparam("offset", type_=Integer()),
    bindparam("limit", type_=Integer()),
)

# 페이지가 범위를 벗어나도 totalCount 를 알 수 있도록 건수는 별도 문장으로 센다.
_COUNT_PAGE = text(
    _FILTERED + "SELECT COUNT(*) AS total_count FROM filtered"
).bindparams(*_FILTER_PARAMS)

# 대표 상품 계산용 상세 줄. 선적 목록·지연 목록과 같은 문장이다.
_LINES = text(
    """
    SELECT l.shipment_id, l.line_no, l.sku_id, l.shipped_quantity,
           k.sku_code, k.product_name
      FROM shipment_lines l
      JOIN skus k ON k.company_id = l.company_id AND k.sku_id = l.sku_id
     WHERE l.company_id = :company_id AND l.shipment_id = ANY(:shipment_ids)
     ORDER BY l.shipment_id, l.line_no
    """
).bindparams(
    bindparam("company_id", type_=_UUID),
    bindparam("shipment_ids", type_=ARRAY(_UUID)),
)


@dataclass(frozen=True)
class DecisionFilter:
    """목록 필터. 모두 선택 사항이고 None 이면 그 조건은 통과한다. decided_to 는 그 시각 미만(배타)이다."""

    decided_from: datetime | None = None
    decided_to: datetime | None = None
    decision_type: str | None = None
    status: str | None = None
    decided_by: uuid.UUID | None = None
    run_id: uuid.UUID | None = None
    query: str | None = None


@dataclass(frozen=True)
class DecisionRow:
    """판단 한 건과 실행·선적·사용자·대응안 요약. 대응안은 의견만 쓴 초안이면 None 이다."""

    decision_type: str
    decision_id: uuid.UUID
    decided_at: datetime
    status: str
    run_id: uuid.UUID
    run_code: str | None
    run_status: str
    run_result_class: str | None
    run_failure_code: str | None
    delay_event_id: uuid.UUID
    shipment_id: uuid.UUID
    shipment_code: str
    user_id: uuid.UUID
    display_name: str
    role_code: str
    response_option_id: uuid.UUID | None
    option_type: str | None


@dataclass(frozen=True)
class ShipmentLineRow:
    """선적 상세 한 줄과 SKU 요약 (대표 상품 계산용)"""

    shipment_id: uuid.UUID
    line_no: int
    sku_id: uuid.UUID
    shipped_quantity: int
    sku_code: str
    product_name: str


@dataclass(frozen=True)
class DecisionPage:
    rows: list[DecisionRow]
    total_count: int


def _like_pattern(query: str | None) -> str | None:
    """부분 일치 패턴. 사용자 입력의 LIKE 특수문자(\\ % _)는 글자 그대로 찾도록 이스케이프한다."""
    if query is None:
        return None
    escaped = (
        query.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", _LIKE_ESCAPE + "%")
        .replace("_", _LIKE_ESCAPE + "_")
    )
    return f"%{escaped}%"


def _filter_params(company_id: uuid.UUID, filters: DecisionFilter) -> dict:
    return {
        "company_id": company_id,
        "decided_from": filters.decided_from,
        "decided_to": filters.decided_to,
        "decision_type": filters.decision_type,
        "status": filters.status,
        "decided_by": filters.decided_by,
        "run_id": filters.run_id,
        "pattern": _like_pattern(filters.query),
    }


class DecisionRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # Read
    async def find_page(
        self,
        company_id: uuid.UUID,
        filters: DecisionFilter,
        *,
        offset: int,
        limit: int,
    ) -> DecisionPage:
        """고객사의 판단 이력 한 페이지(decided_at 최신순·decision_id 순)와 같은 필터를 적용한 전체 건수."""
        params = _filter_params(company_id, filters)
        rows = await self.db.execute(
            _LIST_PAGE, {**params, "offset": offset, "limit": limit}
        )
        total_count = await self.db.scalar(_COUNT_PAGE, params)
        return DecisionPage(
            rows=[DecisionRow(**row._mapping) for row in rows],
            total_count=total_count or 0,
        )

    async def find_shipment_lines(
        self, company_id: uuid.UUID, shipment_ids: Sequence[uuid.UUID]
    ) -> list[ShipmentLineRow]:
        """여러 선적의 상세 줄과 SKU 코드·상품명 (선적·줄 번호 순). 선적이 없으면 조회하지 않는다."""
        if not shipment_ids:
            return []
        # 같은 선적이 여러 행에 나오므로 중복을 없애 넘긴다.
        result = await self.db.execute(
            _LINES,
            {
                "company_id": company_id,
                "shipment_ids": list(dict.fromkeys(shipment_ids)),
            },
        )
        return [ShipmentLineRow(**row._mapping) for row in result]
