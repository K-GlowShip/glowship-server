from app.modules.delay_analysis.application.agent.tools.validation_schemas import (
    MAX_EXPLANATION_ATTEMPTS,
    AgentOutputStatus,
    AgentOutputValidation,
    OutputValidationError,
    ValidateAgentOutputInput,
)
from app.modules.delay_analysis.infrastructure.engine.engine_loader import (
    load_engine_module,
)
from app.modules.delay_analysis.infrastructure.engine.explanation_assets import (
    load_explanation_assets,
)

"""
    T12 validate_agent_output — AI 설명 검증 (docs/agent-flow/tool-contracts.md 8절, GLOW-127)
      - 엔진 explain.validator.validate 를 그대로 부른다. 규칙(스키마·근거 경로·수치·금지 표현·유출·길이·추천 일치)을
        서버에서 다시 만들지 않아, 엔진 설명 생성기(generator)의 시도별 검증과 판정이 같다.
      - 동기 순수 함수다(DB·현재 시각·난수·언어 모델 없음). 자산 파일은 해시를 대조해 한 번 읽어 둔 것을 쓴다.
      - 등록표(노드 연결 GLOW-129)에서 (ctx, payload) 래퍼로 감싸 넣는다(T9·T11 순수 함수와 같은 방식).
"""


def validate_agent_output(data: ValidateAgentOutputInput) -> AgentOutputValidation:
    assets = load_explanation_assets()
    validator = load_engine_module("explain.validator")
    found = validator.validate(
        data.output,
        {
            "input_doc": data.input_document,
            "labels": assets["labels"],
            "forbidden": assets["forbidden"],
            "schema": assets["schema"],
            "expected": data.expected_metadata.model_dump(),
            "canary": data.canary,
        },
    )
    errors = [OutputValidationError(code=code, location=loc) for code, loc in found]
    if not errors:
        status = AgentOutputStatus.PASSED
    elif data.expected_metadata.retry_no < MAX_EXPLANATION_ATTEMPTS:
        status = AgentOutputStatus.RETRY
    else:
        status = AgentOutputStatus.FAILED
    return AgentOutputValidation(status=status, errors=errors)
