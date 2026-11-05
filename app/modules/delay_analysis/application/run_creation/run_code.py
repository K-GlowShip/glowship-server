import re
from datetime import UTC, date, datetime

"""
    표시용 실행 ID run_code (명세 v6.2 D14, 마이그레이션 0006)
      - 형식: AN-{기준일 YYMMDD}-{고객사·기준일별 3자리 순번}. 예) AN-260102-003
      - 기준일은 실행 시작 시각(started_at)의 UTC 날짜다. serviceflow "기준일 D = 입력 확정일(started_at 의 날짜)"과
        수집 하루 경계(UTC)에 맞춘다. 0006 이 기존 행을 채운 규칙과 같다.
      - 순번은 고객사 잠금(L1) 안에서 그날 가장 큰 순번 + 1 로 붙인다. 999 를 넘으면 형식에 담을 수 없다.
"""

RUN_CODE_PREFIX = "AN"
MAX_DAILY_SEQ = 999
_RUN_CODE = re.compile(r"^AN-(\d{6})-(\d{3})$")


def basis_date(started_at: datetime) -> date:
    """run_code 의 날짜 부분이 되는 기준일 (started_at 의 UTC 날짜)"""
    if started_at.tzinfo is None:
        raise ValueError("started_at 은 시간대가 있는 시각이어야 합니다")
    return started_at.astimezone(UTC).date()


def day_prefix(basis: date) -> str:
    """그날 run_code 의 앞부분. 예) AN-260102-"""
    return f"{RUN_CODE_PREFIX}-{basis:%y%m%d}-"


def format_run_code(basis: date, seq: int) -> str:
    if not 1 <= seq <= MAX_DAILY_SEQ:
        raise ValueError(f"run_code 순번은 1~{MAX_DAILY_SEQ} 이어야 합니다: {seq}")
    return f"{day_prefix(basis)}{seq:03d}"


def next_run_code(basis: date, latest_code_of_day: str | None) -> str | None:
    """그날 가장 큰 run_code 다음 값. 그날 실행이 없으면 001, 999 를 다 쓰면 None"""
    if latest_code_of_day is None:
        return format_run_code(basis, 1)
    match = _RUN_CODE.match(latest_code_of_day)
    if match is None or not latest_code_of_day.startswith(day_prefix(basis)):
        raise ValueError(
            f"기준일 {basis} 의 run_code 형식이 아닙니다: {latest_code_of_day}"
        )
    seq = int(match.group(2)) + 1
    return format_run_code(basis, seq) if seq <= MAX_DAILY_SEQ else None
