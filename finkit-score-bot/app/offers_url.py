from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app import storage
from app.config import get_settings


def build_offers_url(base_url: str, threshold: float) -> str:
    parts = urlsplit(base_url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    threshold_value = _query_value(threshold)
    updated_query: list[tuple[str, str]] = []
    replaced = False

    for key, value in query:
        if key == "borrower_score_min":
            if not replaced:
                updated_query.append((key, threshold_value))
                replaced = True
            continue
        updated_query.append((key, value))

    if not replaced:
        updated_query.append(("borrower_score_min", threshold_value))

    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(updated_query),
            parts.fragment,
        )
    )


def current_offers_url() -> str:
    settings = get_settings()
    threshold = storage.get_threshold(settings.default_score_threshold)
    return build_offers_url(settings.finkit_offers_url, threshold)


def _query_value(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return str(value)
