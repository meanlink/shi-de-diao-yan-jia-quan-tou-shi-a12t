from pydantic import BaseModel, Field


class HandoverItemInput(BaseModel):
    day: str = Field(max_length=10)
    team: str = Field(max_length=20)
    zone: str = Field(max_length=60)
    passed: int = Field(ge=0, le=1_000_000_000)
    observed: int = Field(ge=0, le=1_000_000_000)


class HandoverInput(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    source: str = Field(default='纸质交接清单', max_length=200)
    note: str = Field(default='', max_length=500)
    items: list[HandoverItemInput] = Field(min_length=1, max_length=5000)


class AttachInput(BaseModel):
    handover_id: int


class ClaimLine(BaseModel):
    row_index: int = Field(ge=0)
    item_id: int = Field(ge=1)
    passed: int | None = Field(default=None, ge=0, le=1_000_000_000)
    observed: int | None = Field(default=None, ge=0, le=1_000_000_000)


class ClaimInput(BaseModel):
    lines: list[ClaimLine] = Field(min_length=1, max_length=5000)
    idempotency_key: str | None = Field(default=None, max_length=100)


class ConfirmInput(BaseModel):
    idempotency_key: str | None = Field(default=None, max_length=120)
