from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _ensure_paise_int(v):
    """Reject floats/bools/strings - amount_paise must always be a plain integer."""
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError("must be an integer number of paise (no floats, no strings)")
    return v


class WalletCreateRequest(BaseModel):
    initial_balance_paise: int = Field(default=0, ge=0)

    @field_validator("initial_balance_paise", mode="before")
    @classmethod
    def _validate_initial_balance(cls, v):
        return _ensure_paise_int(v)


class WalletResponse(BaseModel):
    id: str
    balance_paise: int
    created_at: datetime


class TransferCreateRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_wallet: str = Field(alias="from")
    to_wallet: str = Field(alias="to")
    amount_paise: int = Field(gt=0)
    idempotency_key: str = Field(min_length=1, max_length=255)

    @field_validator("amount_paise", mode="before")
    @classmethod
    def _validate_amount(cls, v):
        return _ensure_paise_int(v)


class TransferResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    from_wallet: str = Field(alias="from")
    to_wallet: str = Field(alias="to")
    amount_paise: int
    status: str
    decline_reason: Optional[str] = None
    created_at: datetime
    completed_at: Optional[datetime] = None
