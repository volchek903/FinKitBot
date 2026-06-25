from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from typing import Any

from app.config import get_settings
from app.user_filters import USER_FILTER_QUERY_KEYS

LEGACY_USER_FILTER_QUERY_KEYS = ("invest_min", "invest_max")
STRIPPED_USER_FILTER_QUERY_KEYS = USER_FILTER_QUERY_KEYS + LEGACY_USER_FILTER_QUERY_KEYS


def build_offers_url(
    base_url: str,
    threshold: float,
    filters: dict[str, Any] | None = None,
) -> str:
    parts = urlsplit(base_url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    filter_query = _filter_query_pairs(_effective_link_filters(threshold, filters))
    updated_query: list[tuple[str, str]] = []
    replaced = False

    for key, value in query:
        if key in STRIPPED_USER_FILTER_QUERY_KEYS:
            if not replaced:
                updated_query.extend(filter_query)
                replaced = True
            continue
        updated_query.append((key, value))

    if not replaced:
        updated_query.extend(filter_query)

    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(updated_query, doseq=True),
            parts.fragment,
        )
    )


def current_offers_url() -> str:
    settings = get_settings()
    return strip_user_filter_query(settings.finkit_offers_url)


def strip_user_filter_query(base_url: str) -> str:
    parts = urlsplit(base_url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    updated_query = [(key, value) for key, value in query if key not in STRIPPED_USER_FILTER_QUERY_KEYS]
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(updated_query),
            parts.fragment,
        )
    )


def _effective_link_filters(
    threshold: float,
    filters: dict[str, Any] | None,
) -> dict[str, Any]:
    resolved_filters = dict(filters or {})
    if resolved_filters.get("borrower_score_min") is None:
        resolved_filters["borrower_score_min"] = threshold
    return resolved_filters


def _filter_query_pairs(filters: dict[str, Any]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for key in USER_FILTER_QUERY_KEYS:
        if key not in filters:
            continue
        value = filters[key]
        if value is None:
            continue
        if isinstance(value, list):
            for item in value:
                pairs.append((key, _query_value(item)))
            continue
        pairs.append((key, _query_value(value)))
    return pairs


def _query_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return str(value)
    return str(value)
