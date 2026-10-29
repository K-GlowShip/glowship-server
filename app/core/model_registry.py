from app.core.orm import Base
from app.modules.company.infrastructure.admin_assignment import AdminAssignment
from app.modules.company.infrastructure.company import Company
from app.modules.data_sync.infrastructure.data_change_record import DataChangeRecord
from app.modules.data_sync.infrastructure.data_sync_run import DataSyncRun
from app.modules.data_sync.infrastructure.data_validation_issue import (
    DataValidationIssue,
)
from app.modules.decision.infrastructure.final_decision import FinalDecision
from app.modules.decision.infrastructure.member_draft import MemberDraft
from app.modules.delay_analysis.infrastructure.analysis_run import AnalysisRun
from app.modules.delay_analysis.infrastructure.analysis_run_data_version import (
    AnalysisRunDataVersion,
)
from app.modules.delay_analysis.infrastructure.at_risk_order import AtRiskOrder
from app.modules.delay_analysis.infrastructure.delay_event import DelayEvent
from app.modules.delay_analysis.infrastructure.inventory_impact_result import (
    InventoryImpactResult,
)
from app.modules.delay_analysis.infrastructure.option_order_outcome import (
    OptionOrderOutcome,
)
from app.modules.delay_analysis.infrastructure.response_option import ResponseOption
from app.modules.inventory.infrastructure.inventory_daily import InventoryDaily
from app.modules.inventory.infrastructure.regular_order_review import (
    RegularOrderReview,
)
from app.modules.master_data.infrastructure.external_entity_mapping import (
    ExternalEntityMapping,
)
from app.modules.master_data.infrastructure.market_leadtime_reference import (
    MarketLeadtimeReference,
)
from app.modules.master_data.infrastructure.sku import Sku
from app.modules.master_data.infrastructure.warehouse import Warehouse
from app.modules.operating_rule.infrastructure.operating_lead_time import (
    OperatingLeadTime,
)
from app.modules.operating_rule.infrastructure.operating_rule_set import (
    OperatingRuleSet,
)
from app.modules.order.infrastructure.order import Order
from app.modules.order.infrastructure.order_dispatch_date import OrderDispatchDate
from app.modules.order.infrastructure.order_line import OrderLine
from app.modules.order.infrastructure.order_wait_history import OrderWaitHistory
from app.modules.shipment.infrastructure.shipment import Shipment
from app.modules.shipment.infrastructure.shipment_line import ShipmentLine
from app.modules.shipment.infrastructure.shipment_schedule_snapshot import (
    ShipmentScheduleSnapshot,
)
from app.modules.user.infrastructure.user import User

"""
    ORM 모델 등록
      - 모든 모듈의 ORM 모델을 import 해 Base.metadata 에 테이블을 등록합니다.
      - Alembic autogenerate 와 합성데이터 적재 스크립트가 이 모듈의 Base 를 사용합니다.
      - 새 모델을 추가하면 이 파일에도 import 를 추가해야 마이그레이션에 반영됩니다.
"""

__all__ = [
    "AdminAssignment",
    "AnalysisRun",
    "AnalysisRunDataVersion",
    "AtRiskOrder",
    "Base",
    "Company",
    "DataChangeRecord",
    "DataSyncRun",
    "DataValidationIssue",
    "DelayEvent",
    "ExternalEntityMapping",
    "FinalDecision",
    "InventoryDaily",
    "InventoryImpactResult",
    "MarketLeadtimeReference",
    "MemberDraft",
    "OperatingLeadTime",
    "OperatingRuleSet",
    "OptionOrderOutcome",
    "Order",
    "OrderDispatchDate",
    "OrderLine",
    "OrderWaitHistory",
    "RegularOrderReview",
    "ResponseOption",
    "Shipment",
    "ShipmentLine",
    "ShipmentScheduleSnapshot",
    "Sku",
    "User",
    "Warehouse",
]
