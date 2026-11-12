import logging
import sys

"""
    서버 로그 설정 (LOG_LEVEL)
      - 서버에 로깅 설정이 없으면 uvicorn 기본 설정에서는 루트에 출력이 없어, 앱의 INFO 로그(분석 실행 시작·끝, 엔진 저장소
        읽기 시간, 서버 시작 정리 등)가 찍히지 않고 WARNING 이상만 파이썬 기본 출력으로 나온다.
        자동 시작을 켠 dev 서버에서 실행을 따라갈 수 없어(docs/analysis/auto-start.md 7절) 앱 로거만 LOG_LEVEL 로 연다.
      - 앱 로거("app" 아래)만 수준을 정한다. 라이브러리 로거는 루트 수준(WARNING)을 따라 지금과 같다. 특히 모델 호출
        라이브러리의 INFO 에는 프롬프트가 들어가므로 열지 않는다(app/core/llm.py 가 WARNING 으로 고정).
      - 루트에 출력이 없을 때만 표준 오류 출력을 하나 붙인다. 시험(pytest)처럼 이미 붙어 있으면 붙이지 않고, 두 번 불러도
        하나만 붙는다. uvicorn 로거와 모델 사용량 로거는 위로 올려 보내지 않아 같은 줄이 두 번 나오지 않는다.
      - 이 출력은 앱 로그만 그 수준대로 내보내고, 그 밖의 로그는 WARNING 이상만 내보낸다(지금까지 파이썬 기본 출력이 하던 것과 같음).
        SQLAlchemy 엔진(app/core/database.py echo=True)은 자기 출력을 따로 붙여 SQL 을 INFO 로 내므로, 거르지 않으면 같은 SQL 이
        두 번 찍힌다(11-13 uvicorn 으로 확인).
      - 앱 INFO 로그에는 실행 ID·상태·순번·초·모델 이름만 넣는다(NFR-006, 개인정보 없음).
"""

APP_LOGGER_NAME = "app"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class _AppLogHandler(logging.StreamHandler):
    """이 모듈이 붙인 출력인지 알아보는 표지(두 번 붙이지 않게)."""


def _app_or_warning(record: logging.LogRecord) -> bool:
    """앱 로그는 로거 수준이 이미 걸렀으니 그대로, 그 밖은 WARNING 이상만."""
    is_app = record.name == APP_LOGGER_NAME or record.name.startswith(
        APP_LOGGER_NAME + "."
    )
    return is_app or record.levelno >= logging.WARNING


def configure_app_logging(level: str) -> None:
    logging.getLogger(APP_LOGGER_NAME).setLevel(level)
    root = logging.getLogger()
    if root.handlers:
        return
    handler = _AppLogHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(_app_or_warning)
    root.addHandler(handler)
