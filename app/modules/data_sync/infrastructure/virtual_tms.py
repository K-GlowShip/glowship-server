import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

"""
    가상 TMS (GLOW-49, 작업정리본 할 일 8)
      - 실제 포워더 TMS 대신, 날짜별 TMS 응답을 담은 시나리오 파일을 읽어 "그 시각까지 관측된 선적"을 돌려줍니다.
      - 파일 모양은 census source_samples/shipments_api.json 과 같습니다:
            {"interface": "GET /v1/shipments", "responses": [
                {"fetched_at": "2026-07-02T00:00:00Z", "_demo_company": "A_BEAUTY", "data": [ {선적}, ... ]}, ...]}
        응답 하나는 그 시각에 TMS 가 보낸 선적들(바뀐 선적만 와도 됨)이고, 선적 필드는 TMS 응답 그대로입니다(tms_parser).
        "_demo" 로 시작하는 필드는 데모 메타데이터입니다. _demo_company 는 응답을 받는 고객사 코드(companies.company_code)입니다.
      - shipments_between(고객사, after, until): after < fetched_at ≤ until 인 응답을 관측 시각 순서대로 모아,
        같은 선적(shipmentNo)은 가장 늦게 관측된 값 하나만 남깁니다. 한 번의 수집에 같은 선적이 두 번 들어가면
        수집 서비스가 첫 레코드만 적재하므로(DUPLICATE_SOURCE_RECORD) 여기서 최신 값으로 합칩니다.
      - 시각(until)은 호출 측이 7월 초 가상 시각으로 넘깁니다. 서버의 지금 시각(11월)을 넘기면 안 됩니다
        (가상 시각 계산은 수동 수집 연결에서 합니다).
      - 시나리오 파일의 내용(어떤 선적·지연을 넣을지)은 데이터 설계 몫이며 이 모듈은 정하지 않습니다.
"""


class VirtualTmsScenarioError(ValueError):
    """시나리오 파일 형식이 잘못됨 (원천 레코드 거부가 아니라 설정 오류)"""


@dataclass(frozen=True)
class TmsResponse:
    fetched_at: datetime
    company_code: str
    data: tuple[Mapping[str, Any], ...]


class VirtualTmsScenario:
    def __init__(self, responses: Sequence[TmsResponse]):
        # 같은 시각의 응답은 파일 순서를 지킵니다 (sorted 는 안정 정렬)
        self.responses = tuple(sorted(responses, key=lambda r: r.fetched_at))

    @classmethod
    def from_file(cls, path: str | Path) -> VirtualTmsScenario:
        with open(path, encoding="utf-8") as file:
            return cls.from_json(json.load(file))

    @classmethod
    def from_json(cls, document: Any) -> VirtualTmsScenario:
        if not isinstance(document, Mapping) or not isinstance(
            document.get("responses"), list
        ):
            raise VirtualTmsScenarioError("시나리오 최상위에 responses 배열이 없음")
        return cls(
            [_response(raw, index) for index, raw in enumerate(document["responses"])]
        )

    def shipments_between(
        self, company_code: str, after: datetime | None, until: datetime
    ) -> list[Mapping[str, Any]]:
        """after < fetched_at ≤ until 인 이 고객사 응답의 선적. 같은 선적은 마지막 관측 값만, 마지막 관측 순서대로"""
        _require_aware(until, "until")
        if after is not None:
            _require_aware(after, "after")
        latest: dict[Any, Mapping[str, Any]] = {}
        for response in self.responses:
            if response.company_code != company_code:
                continue
            if response.fetched_at > until or (
                after is not None and response.fetched_at <= after
            ):
                continue
            for index, shipment in enumerate(response.data):
                # shipmentNo 가 없는 레코드는 합치지 않고 그대로 넘겨 파서가 거부하게 합니다
                code = (
                    shipment.get("shipmentNo")
                    if isinstance(shipment, Mapping)
                    else None
                )
                key = code if isinstance(code, str) else (response.fetched_at, index)
                latest.pop(key, None)
                latest[key] = shipment
        return list(latest.values())


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None:
        raise ValueError(f"{name} 는 시간대가 있는 시각이어야 함 ({value!r})")


def _response(raw: Any, index: int) -> TmsResponse:
    where = f"responses[{index}]"
    if not isinstance(raw, Mapping):
        raise VirtualTmsScenarioError(f"{where} 가 JSON 객체가 아님")
    company = raw.get("_demo_company")
    if not isinstance(company, str) or not company.strip():
        raise VirtualTmsScenarioError(f"{where}._demo_company(고객사 코드) 없음")
    fetched = raw.get("fetched_at")
    try:
        fetched_at = (
            datetime.fromisoformat(fetched) if isinstance(fetched, str) else None
        )
    except ValueError:
        fetched_at = None
    if fetched_at is None or fetched_at.tzinfo is None:
        raise VirtualTmsScenarioError(
            f"{where}.fetched_at 을 시간대 있는 ISO 8601 시각으로 읽을 수 없음 ({fetched!r})"
        )
    data = raw.get("data")
    if not isinstance(data, list):
        raise VirtualTmsScenarioError(f"{where}.data 가 배열이 아님")
    return TmsResponse(fetched_at=fetched_at, company_code=company, data=tuple(data))
