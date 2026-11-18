import re

"""
    설명 검증 실패 위치 거르기 (docs/agent-flow/explanation.md 7절 7번·8.14절, 문서와 다르게 된 것 DV3-7)
      - 엔진 검증기는 실패마다 "코드@위치"를 낸다. 위치는 대부분 고정 어휘(칸 이름·문장 번호·근거 번호)지만,
        모델이 만든 글자가 들어갈 수 있다: 스키마 검사가 모르는 키 이름을 그대로 넣고(`/metadata`·`/option_explanations/0/explanation`),
        경고 칸은 모델이 적은 경고 코드를 그대로 넣는다(`warnings[<모델이 쓴 코드>]`).
      - 실제 모델 측정 회차의 실패 위치 369개 가운데 21개(7가지, 11-13 초기 회차 하나)가 모델이 지어낸 키 이름이었다(11-17 셈).
      - 이 위치는 실패 요약 문구(failure_message: DB·API 응답 failureMessage)와 내부 추적(EXPLANATION_ATTEMPT)으로 이어진다.
        웹 화면은 설명 실패에 고정 문구를 쓰므로(docs/frontend-api.md 실패 코드 표) 화면에는 나오지 않는다.
        "원문을 넣지 않는다"는 규칙대로 아는 어휘만 그대로 두고, 나머지는 위치를 "형식"으로 바꾼다. 코드는 엔진이 내는 코드(ENGINE_CODES)가 아니면 "?"로 바꾼다.
      - 아는 어휘는 explanation.v1 스키마의 키, 라벨 사전(ko-v1)의 대응안·경고 코드, 엔진 검증기(validator.py)가 쓰는 꼬리표뿐이다.
        엔진 새 판이 위치 모양을 늘리면 그 위치는 "형식"으로 보인다(test_explanation_locations: 검증기가 내는 모양과 실제 측정 위치로 확인).
"""

UNKNOWN_LOCATION = "형식"
UNKNOWN_CODE = "?"

# explanation.v1 스키마 최상위 키 17개와 배열 항목 키(엔진 schemas/explanation.v1.schema.json)
SCHEMA_TOP_KEYS = (
    "schema_version",
    "language",
    "analysis_run_id",
    "decision_signature_hash",
    "model_provider",
    "model_id",
    "prompt_template_id",
    "prompt_template_version",
    "prompt_template_sha256",
    "generated_at",
    "retry_no",
    "run_summary",
    "risk_summary",
    "option_explanations",
    "recommendation_explanation",
    "warnings",
    "evidence_references",
)
SCHEMA_ITEM_KEYS = ("option_type", "text", "code", "id", "path", "value")
# 라벨 사전 ko-v1 의 대응안·경고 코드(엔진 labels/ko-v1.json)
OPTION_CODES = (
    "KEEP_PLAN",
    "PRE_DEPARTURE_AIR",
    "KR_AIR_REPLENISH",
    "OTHER_REGION_FULFILL",
)
WARNING_CODES = (
    "FORECAST_EVENT_OVERLAP",
    "BASELINE_SHORTAGE_EXISTS",
    "OPTION_NEEDS_CHECK",
    "SKU_NOT_ESTIMABLE",
)
# 엔진 validator.META_KEYS
META_KEYS = (
    "analysis_run_id",
    "model_provider",
    "model_id",
    "prompt_template_id",
    "prompt_template_version",
    "prompt_template_sha256",
    "generated_at",
    "retry_no",
    "schema_version",
)

_alt = "|".join
_FIELD = r"(?:run_summary|risk_summary|recommendation_explanation|option_explanations|warnings)"
_BRACKET = rf"(?:\[(?:{_alt(OPTION_CODES + WARNING_CODES)})\])?"
# 꼬리: 문장 번호(#s2), 라벨·문장 수·경고 집합, 금지 표현 항목 번호(:PF-01), 가리킨 근거가 없는 표지(#s2:E9),
# 빠진 근거 경로(:missing /run/…, 엔진이 1,000자에서 자를 수 있음), 빠진 "예측 기준" 문구(:missing 예측 기준)
_TAIL = (
    r"(?:#s\d{1,2})?"
    r"(?::(?:reason_label|status_label|result_class_label|sentences|set|[A-Z]{2}-\d{2}|E\d{1,4}"
    r"|missing 예측 기준|missing [A-Za-z0-9_/.\-]{0,120}))?"
)
_ALLOWED = re.compile(
    "|".join(
        (
            rf"{_FIELD}{_BRACKET}{_TAIL}",
            r"(?:body|json|language|adapter|canary|decision_signature_hash)",
            r"evidence:E\d{1,4}(?::duplicate)?",
            rf"meta:(?:{_alt(META_KEYS)})",
            rf"/(?:{_alt(SCHEMA_TOP_KEYS)})(?:/(?:\d{{1,3}}|{_alt(SCHEMA_ITEM_KEYS)}))*",
        )
    )
)
# 엔진 validator 머리말의 코드 + 생성기가 붙이는 코드(MODEL_CALL_ERROR·MODEL_TIMEOUT)
ENGINE_CODES = frozenset(
    (
        "SCHEMA_INVALID",
        "EVIDENCE_UNRESOLVED",
        "UNREFERENCED_VALUE",
        "VALUE_MISMATCH",
        "SIGNATURE_HASH_MISMATCH",
        "OPTION_SET_MISMATCH",
        "RECOMMENDATION_MISMATCH",
        "FORBIDDEN_EXPRESSION",
        "LENGTH_EXCEEDED",
        "LANGUAGE_INVALID",
        "LEAKAGE_DETECTED",
        "MODEL_CALL_ERROR",
        "MODEL_TIMEOUT",
    )
)
MAX_DETAIL = 1000  # 엔진 failure_message 와 같은 길이(generator: msg[:1000])


def safe_location(location: object) -> str:
    """검증 실패 위치 하나. 아는 어휘면 그대로, 아니면 "형식"."""
    text = str(location) if location is not None else ""
    return text if _ALLOWED.fullmatch(text) else UNKNOWN_LOCATION


def safe_code(code: object) -> str:
    """검증 실패 코드 하나. 엔진이 내는 코드면 그대로, 아니면 "?"."""
    text = str(code) if code is not None else ""
    return text if text in ENGINE_CODES else UNKNOWN_CODE


def safe_code_location(code: object, location: object) -> str:
    return f"{safe_code(code)}@{safe_location(location)}"


def safe_failure_message(attempts: object) -> str:
    """
    엔진 시도 기록(attempts: [{retry_no, codes: [[코드, 위치], …]}, …])으로 failure_message 를 다시 만든다.
    엔진 문자열(generator failure_message)과 같은 모양("1:코드@위치|…;2:…")이지만 코드·위치는 걸러진 것이다.
    문자열을 잘라 읽지 않으므로 위치 안에 ";"·"|"·"@" 가 있어도 모양이 깨지지 않는다.
    """
    parts = []
    for attempt in attempts if isinstance(attempts, (list, tuple)) else []:
        if not isinstance(attempt, dict):
            continue
        number = attempt.get("retry_no")
        number_text = str(number) if isinstance(number, int) else UNKNOWN_CODE
        codes = []
        for pair in attempt.get("codes") or []:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                codes.append(safe_code_location(pair[0], pair[1]))
            else:
                codes.append(f"{UNKNOWN_CODE}@{UNKNOWN_LOCATION}")
        parts.append(f"{number_text}:{'|'.join(codes)}")
    return ";".join(parts)[:MAX_DETAIL]


def safe_attempt_detail(detail: str | None) -> str | None:
    """
    엔진 generator 의 failure_message("1:코드@위치|코드@위치;2:…;3:…")를 같은 모양으로 걸러 돌려준다.
    모양을 모르는 조각(엔진이 1,000자에서 잘라 끝이 깨진 조각 포함)은 그 조각만 "?@형식"으로 바꾼다.
    """
    if not detail:
        return detail
    attempts = []
    for attempt in detail.split(";"):
        number, sep, items = attempt.partition(":")
        if not sep or not number.strip().isdigit():
            attempts.append(f"?:{UNKNOWN_CODE}@{UNKNOWN_LOCATION}")
            continue
        parts = []
        for item in items.split("|"):
            code, at, location = item.partition("@")
            parts.append(
                safe_code_location(code, location)
                if at
                else f"{UNKNOWN_CODE}@{UNKNOWN_LOCATION}"
            )
        attempts.append(f"{number.strip()}:{'|'.join(parts)}")
    return ";".join(attempts)
