from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.config import MAX_PAISE
from app.models import TransferIn


@pytest.mark.parametrize("amount", [True, False, 1.0, 1.25, "100", 0, -1, MAX_PAISE + 1, None])
def test_money_is_strict_positive_integer(amount):
    with pytest.raises(ValidationError):
        TransferIn.model_validate(
            {"from": str(uuid4()), "to": str(uuid4()), "amount_paise": amount, "idempotency_key": "k"}
        )


def test_self_transfer_and_unknown_fields_rejected():
    wid = str(uuid4())
    with pytest.raises(ValidationError):
        TransferIn.model_validate({"from": wid, "to": wid, "amount_paise": 1, "idempotency_key": "k"})
    with pytest.raises(ValidationError):
        TransferIn.model_validate(
            {"from": wid, "to": str(uuid4()), "amount_paise": 1, "idempotency_key": "k", "currency": "INR"}
        )


def test_full_supported_range_is_exact():
    body = TransferIn.model_validate(
        {"from": str(uuid4()), "to": str(uuid4()), "amount_paise": MAX_PAISE, "idempotency_key": "k"}
    )
    assert body.amount_paise == MAX_PAISE and type(body.amount_paise) is int
