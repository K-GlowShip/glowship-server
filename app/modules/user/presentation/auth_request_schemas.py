from pydantic import BaseModel, Field, field_validator


class EmailLoginRequest(BaseModel):
    email: str = Field(
        max_length=50,
        pattern=r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$",
        examples=["admin@glowship.com"],
    )
    password: str = Field(examples=["cGFzc3dvcmQxMjM="])

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        """대소문자만 정규화한다. 공백은 허용하지 않으며, 위 pattern 검사가 먼저 실행되어 422 로 거절된다."""
        return v.lower()
