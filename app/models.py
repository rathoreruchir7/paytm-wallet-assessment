from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import MAX_PAISE

Paise = Annotated[int, Field(strict=True, ge=1, le=MAX_PAISE)]


class EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TransferIn(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=False)
    from_wallet: UUID = Field(alias="from")
    to_wallet: UUID = Field(alias="to")
    amount_paise: Paise
    idempotency_key: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")]

    @model_validator(mode="after")
    def distinct_wallets(self):
        if self.from_wallet == self.to_wallet:
            raise ValueError("source and destination must differ")
        return self


class FixtureIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: Annotated[int, Field(strict=True, ge=2, le=10)] = 4
    initial_balance_paise: Annotated[int, Field(strict=True, ge=0, le=MAX_PAISE)] = 10_000
