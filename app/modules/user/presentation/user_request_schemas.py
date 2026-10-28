from pydantic import BaseModel, Field


class EmailLoginRequest(BaseModel):
    email: str = Field(
        pattern=r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$",
        examples=["admin@glowship.com"],
    )
    password: str = Field(examples=["cGFzc3dvcmQxMjM="])
