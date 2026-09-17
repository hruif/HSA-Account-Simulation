"""Request bodies. Pydantic rejects bad input with 422 before any service runs."""
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from categories import ALL_CATEGORIES

MAX_AMOUNT_CENTS = 1_000_000_00  # $1,000,000 per request


def _fits_bcrypt(password: str) -> str:
    if len(password.encode()) > 72:
        raise ValueError("must be at most 72 bytes")
    return password


def _known_category(category: str) -> str:
    if category not in ALL_CATEGORIES:
        raise ValueError(f"must be one of: {', '.join(ALL_CATEGORIES)}")
    return category


Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Email = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"),
]
Password = Annotated[str, AfterValidator(_fits_bcrypt)]
# Login does not enforce the 72-byte bcrypt limit: a password that is too long is simply
# wrong, and every wrong credential must come back as 401, not 422.
GivenPassword = Annotated[str, StringConstraints(max_length=1024)]
AmountCents = Annotated[int, Field(strict=True, gt=0, le=MAX_AMOUNT_CENTS)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SignupIn(_Body):
    owner_name: Name
    email: Email
    password: Annotated[Password, Field(min_length=8)]


class LoginIn(_Body):
    email: Annotated[str, StringConstraints(strip_whitespace=True, max_length=254)]
    password: GivenPassword


class DepositIn(_Body):
    amount_cents: AmountCents


class PurchaseIn(_Body):
    card_number: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^\d{16}$")]
    merchant_name: Name
    merchant_category: Annotated[str, AfterValidator(_known_category)]
    amount_cents: AmountCents
    # Optional. Send the same one again to retry safely: the purchase is not charged twice.
    request_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)] | None = None
