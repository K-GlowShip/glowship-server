import json
import threading
import uuid
from pathlib import Path

from app.modules.delay_analysis.application.agent.tracing import TraceEvent

"""
    추적 내부 로그 (agentflow 8.1 "전송 실패"·"외부 전송 불허 시")
      - 사건 하나 = JSON 한 줄. 경로: <base>/agent_trace_outbox/<company_id>/<YYYY-MM-DD>.jsonl (사건 시각의 UTC 날짜)
      - LangSmith 전송이 꺼져 있거나 허용되지 않으면 같은 구조의 기록을 여기에 전량 남긴다. 재전송은 이 파일을 읽어서 한다(전송기 작업)
      - 보존 기간·회전·암호화·운영 주체는 운영 확인 항목이다(정해지기 전에는 파일을 지우지 않음)
"""

OUTBOX_DIR_NAME = "agent_trace_outbox"


class TraceOutboxSink:
    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self._lock = threading.Lock()

    def path_for(self, company_id: uuid.UUID, at: str) -> Path:
        return self.base_dir / OUTBOX_DIR_NAME / str(company_id) / f"{at[:10]}.jsonl"

    def write(self, event: TraceEvent) -> None:
        path = self.path_for(event.company_id, event.at)
        line = json.dumps(event.to_json_dict(), ensure_ascii=False, sort_keys=True)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="\n") as file:
                file.write(line + "\n")
