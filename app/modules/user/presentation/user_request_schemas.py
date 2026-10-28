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
        return v.strip().lower()
