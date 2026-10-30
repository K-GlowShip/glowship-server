from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

"""
    요청·응답 스키마 공통 베이스

    API 명세(D2)에 따라 JSON 필드는 camelCase 로 주고받고, 파이썬 코드는 snake_case 를 유지한다.
      - alias_generator: snake_case 필드 이름 → camelCase 별칭
      - validate_by_alias / validate_by_name: 요청은 camelCase 로 받고, 파이썬 코드에서는 snake_case 로도 만들 수 있다
      - serialize_by_alias: 응답은 camelCase 로 직렬화한다 (FastAPI 의 response_model 도 별칭을 쓴다)
      - from_attributes: ORM 객체를 model_validate 로 바로 응답 스키마에 담을 수 있다
"""


class CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        validate_by_alias=True,
        validate_by_name=True,
        serialize_by_alias=True,
        from_attributes=True,
    )
