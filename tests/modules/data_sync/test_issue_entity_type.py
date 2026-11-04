import re

from sqlalchemy import CheckConstraint

from app.modules.data_sync.infrastructure.data_change_record import DataChangeRecord
from app.modules.data_sync.infrastructure.data_sync_enums import (
    ISSUE_ENTITY_TYPE_OF,
    IssueEntityType,
    SourceDomain,
)

"""
    검증 이슈 엔터티 유형 (명세 v6.2 4.3 C21, DB 없음)
"""


def test_every_source_domain_has_an_entity_type():
    assert set(ISSUE_ENTITY_TYPE_OF) == set(SourceDomain)
    assert ISSUE_ENTITY_TYPE_OF[SourceDomain.PRODUCT] == IssueEntityType.SKU


def test_entity_types_are_ddl_entity_names():
    """변경 기록 entity_type(data_change_records_ck1) 중 부모 엔터티 이름과 같음 (상세 줄 _LINE 은 쓰지 않음)"""
    [check] = [
        c
        for c in DataChangeRecord.__table__.constraints
        if isinstance(c, CheckConstraint) and c.name == "data_change_records_ck1"
    ]
    ddl_names = set(re.findall(r"'([^']+)'", str(check.sqltext)))

    assert {t.value for t in IssueEntityType} == ddl_names - {
        "ORDER_LINE",
        "SHIPMENT_LINE",
    }
