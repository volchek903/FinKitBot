import hashlib
import json
import re
from datetime import datetime
from typing import Any, Iterable
from urllib.parse import urljoin

from app.models import Offer

TABLE_SELECTOR = "table.p-datatable-table"
ROW_SELECTOR = "table.p-datatable-table tbody tr"
CELL_SELECTOR = "td"
COLUMNS = [
    "amount",
    "term",
    "interest_rate",
    "borrower_rating",
    "borrower_score",
    "borrower",
    "signed_at",
    "expected_income",
    "actions",
]

SCORE_ALIASES = {
    "borrower_score",
    "score",
    "scorball",
    "scoreBall",
    "score_ball",
    "скоррбал",
    "скорбалл",
    "Скор балл",
}
ID_ALIASES = {
    "id",
    "offerId",
    "offer_id",
    "loanId",
    "loan_id",
    "investmentId",
    "investment_id",
}
FIELD_ALIASES = {
    "amount": ("amount",),
    "term": ("term",),
    "rate": ("interest_rate", "rate"),
    "rating": ("borrower_rating", "rating"),
    "borrower": ("borrower", "borrower_short_name"),
    "signed_at": ("signed_at", "signedAt", "signedAtDate"),
    "expected_income": ("expected_income", "expectedIncome"),
    "status": ("status_display", "status"),
    "url": ("url", "link", "href"),
    "invest": ("invest",),
}
OFFER_FIELD_ALIASES = set().union(*[set(values) for values in FIELD_ALIASES.values()])


def parse_score(value: str | int | float | None) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)

    cleaned = str(value).replace(",", ".")
    cleaned = re.sub(r"[^0-9.]", "", cleaned)
    if not cleaned:
        return None

    if cleaned.count(".") > 1:
        first, *rest = cleaned.split(".")
        cleaned = first + "." + "".join(rest)

    try:
        return float(cleaned)
    except ValueError:
        return None


def clean_borrower(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = str(value)
    cleaned = cleaned.replace("i Профиль", "")
    cleaned = cleaned.replace("Просмотрено", "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or None


def stable_offer_id(
    score: str | int | float | None = None,
    amount: str | None = None,
    term: str | None = None,
    rate: str | None = None,
    rating: str | None = None,
    borrower: str | None = None,
    signed_at: str | None = None,
    expected_income: str | None = None,
    url: str | None = None,
) -> str:
    parts = [
        _normalize_id_part(score),
        _normalize_id_part(amount),
        _normalize_id_part(term),
        _normalize_id_part(rate),
        _normalize_id_part(rating),
        _normalize_id_part(borrower),
        _normalize_id_part(signed_at),
        _normalize_id_part(expected_income),
        _normalize_id_part(url),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def parse_offers_from_json(payload: dict | list) -> list[Offer]:
    offers: list[Offer] = []
    seen: set[str] = set()

    for item in _walk_dicts(payload):
        if not _dict_looks_like_offer(item):
            continue
        offer = _offer_from_dict(item)
        if offer.id in seen:
            continue
        seen.add(offer.id)
        offers.append(offer)

    return offers


def parse_offer_row(
    cells: list[str],
    base_url: str | None = None,
    url: str | None = None,
) -> Offer | None:
    if len(cells) < len(COLUMNS):
        return None

    amount = _clean_text(cells[0])
    term = _clean_text(cells[1])
    rate = _clean_text(cells[2])
    rating = _clean_text(cells[3])
    score = parse_score(cells[4])
    borrower = clean_borrower(cells[5])
    signed_at = _clean_text(cells[6])
    expected_income = _clean_text(cells[7])
    actions = _clean_text(cells[8]) or ""
    status = "available" if "инвестировать" in actions.lower() else "unknown"
    offer_url = urljoin(base_url or "", url or "") if url else None
    offer_id = stable_offer_id(
        score=score,
        amount=amount,
        term=term,
        rate=rate,
        rating=rating,
        borrower=borrower,
        signed_at=signed_at,
        expected_income=expected_income,
        url=offer_url,
    )
    return Offer(
        id=offer_id,
        score=score,
        amount=amount,
        term=term,
        rate=rate,
        rating=rating,
        borrower=borrower,
        signed_at=signed_at,
        expected_income=expected_income,
        status=status,
        url=offer_url,
        raw={"cells": cells},
    )


def parse_offers_from_html(html: str, base_url: str) -> list[Offer]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    offers: list[Offer] = []
    seen: set[str] = set()

    for row in soup.select(ROW_SELECTOR):
        cells = [_clean_text(cell.get_text(" ", strip=True)) or "" for cell in row.select(CELL_SELECTOR)]
        link = row.select_one("a[href]")
        url = link.get("href") if link else None
        offer = parse_offer_row(cells, base_url=base_url, url=url)
        if offer is None or offer.id in seen:
            continue
        seen.add(offer.id)
        offers.append(offer)

    return offers


def _offer_from_dict(item: dict[str, Any]) -> Offer:
    score = parse_score(_get_alias_value(item, SCORE_ALIASES))
    amount = _format_amount(_get_alias_value(item, FIELD_ALIASES["amount"]))
    term = _value_to_str(_get_alias_value(item, FIELD_ALIASES["term"]))
    rate = _format_rate(_get_alias_value(item, FIELD_ALIASES["rate"]))
    rating = _value_to_str(_get_alias_value(item, FIELD_ALIASES["rating"]))
    borrower = clean_borrower(_value_to_str(_get_alias_value(item, FIELD_ALIASES["borrower"])))
    signed_at = _format_signed_at(_get_alias_value(item, FIELD_ALIASES["signed_at"]))
    expected_income = _value_to_str(_get_alias_value(item, FIELD_ALIASES["expected_income"]))
    status = _value_to_str(_get_alias_value(item, FIELD_ALIASES["status"]))
    invest = _get_alias_value(item, FIELD_ALIASES["invest"])
    if status is None and invest is not None:
        status = _status_from_invest(invest)
    if expected_income is None:
        expected_income = _estimate_expected_income(amount, rate, term)
    url = _value_to_str(_get_alias_value(item, FIELD_ALIASES["url"]))
    offer_id = _value_to_str(_get_alias_value(item, ID_ALIASES))
    if not offer_id:
        offer_id = stable_offer_id(
            score=score,
            amount=amount,
            term=term,
            rate=rate,
            rating=rating,
            borrower=borrower,
            signed_at=signed_at,
            expected_income=expected_income,
            url=url,
        )
    return Offer(
        id=offer_id,
        score=score,
        amount=amount,
        term=term,
        rate=rate,
        rating=rating,
        borrower=borrower,
        signed_at=signed_at,
        expected_income=expected_income,
        status=status,
        url=url,
        raw=item,
    )


def _dict_looks_like_offer(item: dict[str, Any]) -> bool:
    normalized_keys = {_normalize_key(key) for key in item.keys()}
    score_keys = {_normalize_key(key) for key in SCORE_ALIASES}
    offer_keys = {_normalize_key(key) for key in OFFER_FIELD_ALIASES}
    if normalized_keys & score_keys:
        return True
    return len(normalized_keys & offer_keys) >= 3


def _walk_dicts(payload: Any) -> Iterable[dict[str, Any]]:
    if isinstance(payload, dict):
        yield payload
        for value in payload.values():
            yield from _walk_dicts(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _walk_dicts(item)


def _get_alias_value(item: dict[str, Any], aliases: Iterable[str]) -> Any:
    normalized_aliases = {_normalize_key(alias) for alias in aliases}
    for key, value in item.items():
        if _normalize_key(key) in normalized_aliases:
            return value
    return None


def _normalize_key(value: str) -> str:
    return re.sub(r"[\s_\-]+", "", str(value).strip().lower())


def _normalize_id_part(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return re.sub(r"\s+", " ", str(value)).strip()


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = re.sub(r"\s+", " ", str(value)).strip()
    return cleaned or None


def _value_to_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in ("name", "title", "full_name", "fio", "value"):
            if key in value:
                return _value_to_str(value[key])
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, list):
        return ", ".join(filter(None, (_value_to_str(item) for item in value))) or None
    cleaned = _clean_text(str(value))
    return cleaned


def _status_from_invest(value: Any) -> str | None:
    if isinstance(value, bool):
        return "available" if value else "disabled"
    text = _value_to_str(value)
    if not text:
        return None
    return text


def _format_amount(value: Any) -> str | None:
    text = _value_to_str(value)
    if text is None or any(marker in text.upper() for marker in ("BYN", "USD", "EUR", "RUB")):
        return text
    amount = parse_score(text)
    if amount is None:
        return text
    return f"{amount:.2f} BYN"


def _format_rate(value: Any) -> str | None:
    text = _value_to_str(value)
    if text is None or "%" in text:
        return text
    rate = parse_score(text)
    if rate is None:
        return text
    return f"{rate:.2f} %"


def _format_signed_at(value: Any) -> str | None:
    text = _value_to_str(value)
    if text is None:
        return None
    if "T" not in text:
        return text
    try:
        return datetime.fromisoformat(text).strftime("%d.%m.%Y")
    except ValueError:
        return text


def _estimate_expected_income(amount: str | None, rate: str | None, term: str | None) -> str | None:
    amount_value = parse_score(amount)
    rate_value = parse_score(rate)
    term_value = parse_score(term)
    if amount_value is None or rate_value is None or term_value is None:
        return None
    return f"{(amount_value * rate_value * term_value / 100):.2f} BYN"
