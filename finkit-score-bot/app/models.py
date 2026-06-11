from dataclasses import dataclass
from typing import Any


@dataclass
class Offer:
    id: str
    score: float | None
    amount: str | None = None
    term: str | None = None
    rate: str | None = None
    rating: str | None = None
    borrower: str | None = None
    signed_at: str | None = None
    expected_income: str | None = None
    status: str | None = None
    url: str | None = None
    raw: Any | None = None

