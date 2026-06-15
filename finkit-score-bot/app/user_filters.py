from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.models import Offer
from app.parser import parse_score

RATING_SCALE = ("A", "B", "C", "D", "E", "F")
AGE_GROUP_OPTIONS = ("18-27", "28-35", "36-49", "50-59")


@dataclass(frozen=True)
class UserFilterField:
    key: str
    label: str
    kind: str
    prompt: str


NUMERIC_LIMITS: dict[str, tuple[float, float]] = {
    "borrower_score_min": (0, 100),
    "borrower_score_max": (0, 100),
    "amount_min": (0, 100000),
    "amount_max": (0, 100000),
    "term_min": (0, 365),
    "term_max": (0, 365),
    "interest_rate_min": (0, 10),
    "interest_rate_max": (0, 10),
    "invest_min": (0, 100000),
    "invest_max": (0, 100000),
}

FILTER_FIELDS: tuple[UserFilterField, ...] = (
    UserFilterField(
        key="borrower_score_min",
        label="Скор от",
        kind="number",
        prompt="Введите минимальный скор балл числом от 0 до 100. Для очистки отправьте `-`.",
    ),
    UserFilterField(
        key="borrower_score_max",
        label="Скор до",
        kind="number",
        prompt="Введите максимальный скор балл числом от 0 до 100. Для очистки отправьте `-`.",
    ),
    UserFilterField(
        key="amount_min",
        label="Сумма от",
        kind="number",
        prompt="Введите минимальную сумму займа числом. Для очистки отправьте `-`.",
    ),
    UserFilterField(
        key="amount_max",
        label="Сумма до",
        kind="number",
        prompt="Введите максимальную сумму займа числом. Для очистки отправьте `-`.",
    ),
    UserFilterField(
        key="term_min",
        label="Срок от",
        kind="number",
        prompt="Введите минимальный срок в днях. Для очистки отправьте `-`.",
    ),
    UserFilterField(
        key="term_max",
        label="Срок до",
        kind="number",
        prompt="Введите максимальный срок в днях. Для очистки отправьте `-`.",
    ),
    UserFilterField(
        key="interest_rate_min",
        label="Ставка от",
        kind="number",
        prompt="Введите минимальную ставку числом. Для очистки отправьте `-`.",
    ),
    UserFilterField(
        key="interest_rate_max",
        label="Ставка до",
        kind="number",
        prompt="Введите максимальную ставку числом. Для очистки отправьте `-`.",
    ),
    UserFilterField(
        key="borrower_rating_min",
        label="Рейтинг от",
        kind="rating",
        prompt="Введите минимальный рейтинг буквой от A до F. Для очистки отправьте `-`.",
    ),
    UserFilterField(
        key="borrower_rating_max",
        label="Рейтинг до",
        kind="rating",
        prompt="Введите максимальный рейтинг буквой от A до F. Для очистки отправьте `-`.",
    ),
    UserFilterField(
        key="invest_min",
        label="Инвест от",
        kind="number",
        prompt="Введите минимальное уже инвестированное значение числом. Для очистки отправьте `-`.",
    ),
    UserFilterField(
        key="invest_max",
        label="Инвест до",
        kind="number",
        prompt="Введите максимальное уже инвестированное значение числом. Для очистки отправьте `-`.",
    ),
    UserFilterField(
        key="borrower_income_confirmed",
        label="Доход подтвержден",
        kind="bool",
        prompt="Введите `да`, `нет` или `-`, чтобы сбросить фильтр.",
    ),
    UserFilterField(
        key="borrower_enforcement_up_to_1_month_absent",
        label="Исп. пр-ва отсутствуют",
        kind="bool",
        prompt="Введите `да`, `нет` или `-`, чтобы сбросить фильтр.",
    ),
    UserFilterField(
        key="borrower_age_group",
        label="Возраст",
        kind="age_group",
        prompt=(
            "Введите возрастную группу: 18-27, 28-35, 36-49, 50-59. "
            "Можно несколько через запятую. Для очистки отправьте `-`."
        ),
    ),
)

FILTER_FIELD_MAP = {field.key: field for field in FILTER_FIELDS}
USER_FILTER_QUERY_KEYS = tuple(field.key for field in FILTER_FIELDS)
BUTTON_LABELS = {
    "borrower_income_confirmed": "Доход",
    "borrower_enforcement_up_to_1_month_absent": "Исп. пр-ва",
}


def resolved_user_filters(raw_filters: dict[str, Any] | None, default_threshold: float) -> dict[str, Any]:
    source = raw_filters or {}
    resolved = empty_user_filters()
    resolved.update(
        {key: source[key] for key in source if key in FILTER_FIELD_MAP and source[key] is not None}
    )
    if resolved.get("borrower_score_min") is None:
        resolved["borrower_score_min"] = default_threshold
    return resolved


def validate_filter_value(field_key: str, raw_value: str, filters: dict[str, Any] | None = None) -> Any:
    field = FILTER_FIELD_MAP[field_key]
    value = raw_value.strip()
    if value in {"", "-"}:
        return None

    if field.kind == "number":
        number = parse_score(value)
        if number is None:
            raise ValueError("Введите число.")
        lower, upper = numeric_input_bounds(field_key, filters or {})
        if number < lower or number > upper:
            raise ValueError(
                f"Допустимо значение от {display_number(lower)} до {display_number(upper)}."
            )
        return number

    if field.kind == "rating":
        normalized = value.upper().strip()
        if normalized not in RATING_SCALE:
            raise ValueError("Введите букву от A до F.")
        lower, upper = rating_input_bounds(field_key, filters or {})
        normalized_index = RATING_SCALE.index(normalized)
        if not (RATING_SCALE.index(lower) <= normalized_index <= RATING_SCALE.index(upper)):
            raise ValueError(f"Допустимо значение от {lower} до {upper}.")
        return normalized

    if field.kind == "bool":
        normalized = value.lower().strip()
        if normalized in {"да", "yes", "true", "1"}:
            return True
        if normalized in {"нет", "no", "false", "0"}:
            return False
        raise ValueError("Введите `да`, `нет` или `-`.")

    if field.kind == "age_group":
        parts = [part.strip() for part in value.split(",") if part.strip()]
        if not parts:
            return None
        invalid = [part for part in parts if part not in AGE_GROUP_OPTIONS]
        if invalid:
            raise ValueError("Допустимые значения: 18-27, 28-35, 36-49, 50-59.")
        return parts

    raise ValueError("Неподдерживаемый тип фильтра.")


def format_filter_value(field_key: str, value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def filter_button_text(field_key: str, value: Any) -> str:
    field = FILTER_FIELD_MAP[field_key]
    label = BUTTON_LABELS.get(field_key, field.label)
    return f"{label}: {format_filter_value(field_key, value)}"


def filter_prompt(field_key: str, filters: dict[str, Any] | None = None) -> str:
    field = FILTER_FIELD_MAP[field_key]
    current_filters = filters or {}

    if field.kind == "number":
        lower, upper = numeric_input_bounds(field_key, current_filters)
        return (
            f"Введите число от {display_number(lower)} до {display_number(upper)}.\n"
            "Для очистки отправьте `-`."
        )

    if field.kind == "rating":
        lower, upper = rating_input_bounds(field_key, current_filters)
        return f"Введите рейтинг от {lower} до {upper}. Для очистки отправьте `-`."

    return field.prompt


def filter_field_label(field_key: str) -> str:
    return FILTER_FIELD_MAP[field_key].label


def numeric_input_bounds(field_key: str, filters: dict[str, Any]) -> tuple[float, float]:
    absolute_min, absolute_max = NUMERIC_LIMITS[field_key]
    pair_key = paired_field_key(field_key)

    if field_key.endswith("_min") and pair_key in filters and filters[pair_key] is not None:
        return absolute_min, min(float(filters[pair_key]), absolute_max)

    if field_key.endswith("_max") and pair_key in filters and filters[pair_key] is not None:
        return max(float(filters[pair_key]), absolute_min), absolute_max

    return absolute_min, absolute_max


def rating_input_bounds(field_key: str, filters: dict[str, Any]) -> tuple[str, str]:
    lower = "A"
    upper = "F"
    pair_key = paired_field_key(field_key)
    pair_value = filters.get(pair_key)
    if not pair_value or pair_value not in RATING_SCALE:
        return lower, upper
    if field_key.endswith("_min"):
        return lower, pair_value
    return pair_value, upper


def paired_field_key(field_key: str) -> str:
    if field_key.endswith("_min"):
        return field_key[:-4] + "_max"
    if field_key.endswith("_max"):
        return field_key[:-4] + "_min"
    return ""


def display_number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return str(value)


def offer_matches_user_filters(offer: Offer, filters: dict[str, Any]) -> bool:
    resolved = resolved_user_filters(filters, default_threshold=0)
    return all(
        (
            _matches_min(_offer_number(offer, "borrower_score"), resolved.get("borrower_score_min")),
            _matches_max(_offer_number(offer, "borrower_score"), resolved.get("borrower_score_max")),
            _matches_min(_offer_number(offer, "amount"), resolved.get("amount_min")),
            _matches_max(_offer_number(offer, "amount"), resolved.get("amount_max")),
            _matches_min(_offer_number(offer, "term"), resolved.get("term_min")),
            _matches_max(_offer_number(offer, "term"), resolved.get("term_max")),
            _matches_min(_offer_number(offer, "interest_rate"), resolved.get("interest_rate_min")),
            _matches_max(_offer_number(offer, "interest_rate"), resolved.get("interest_rate_max")),
            _matches_rating(
                _offer_rating(offer),
                resolved.get("borrower_rating_min"),
                resolved.get("borrower_rating_max"),
            ),
            _matches_min(_offer_number(offer, "invest"), resolved.get("invest_min")),
            _matches_max(_offer_number(offer, "invest"), resolved.get("invest_max")),
            _matches_bool(
                _offer_bool(offer, "borrower_income_confirmed"),
                resolved.get("borrower_income_confirmed"),
            ),
            _matches_enforcement(
                _offer_bool(offer, "borrower_has_enforcement_proceedings"),
                resolved.get("borrower_enforcement_up_to_1_month_absent"),
            ),
            _matches_age_group(_offer_age_group(offer), resolved.get("borrower_age_group")),
        )
    )


def empty_user_filters() -> dict[str, Any]:
    return {
        "borrower_score_min": 0,
        "amount_min": 0,
        "term_min": 0,
        "interest_rate_min": 0,
        "invest_min": 0,
    }


def _matches_min(actual: float | None, expected_min: Any) -> bool:
    if expected_min is None:
        return True
    if float(expected_min) <= 0:
        return True
    if actual is None:
        return False
    return actual >= float(expected_min)


def _matches_max(actual: float | None, expected_max: Any) -> bool:
    if expected_max is None:
        return True
    if actual is None:
        return False
    return actual <= float(expected_max)


def _matches_bool(actual: bool | None, expected: Any) -> bool:
    if expected is None:
        return True
    if actual is None:
        return False
    return actual is bool(expected)


def _matches_enforcement(actual_has_enforcement: bool | None, expected_absent: Any) -> bool:
    if expected_absent is None:
        return True
    if actual_has_enforcement is None:
        return False
    return (not actual_has_enforcement) is bool(expected_absent)


def _matches_age_group(actual: str | None, expected: Any) -> bool:
    if expected is None:
        return True
    if actual is None:
        return False
    expected_values = expected if isinstance(expected, list) else [expected]
    return actual in expected_values


def _matches_rating(actual: str | None, lower: Any, upper: Any) -> bool:
    if lower is None and upper is None:
        return True
    if actual is None:
        return False
    actual_value = actual.upper().strip()
    if actual_value not in RATING_SCALE:
        return False

    low_index = RATING_SCALE.index(lower) if lower in RATING_SCALE else 0
    high_index = RATING_SCALE.index(upper) if upper in RATING_SCALE else len(RATING_SCALE) - 1
    if low_index > high_index:
        low_index, high_index = high_index, low_index
    actual_index = RATING_SCALE.index(actual_value)
    return low_index <= actual_index <= high_index


def _offer_number(offer: Offer, field_name: str) -> float | None:
    raw = offer.raw if isinstance(offer.raw, dict) else {}
    if field_name == "borrower_score":
        return offer.score
    if field_name == "amount":
        return parse_score(raw.get("amount", offer.amount))
    if field_name == "term":
        return parse_score(raw.get("term", offer.term))
    if field_name == "interest_rate":
        return parse_score(raw.get("interest_rate", offer.rate))
    if field_name == "invest":
        return parse_score(raw.get("invest", raw.get("total_invested")))
    return None


def _offer_bool(offer: Offer, field_name: str) -> bool | None:
    raw = offer.raw if isinstance(offer.raw, dict) else {}
    value = raw.get(field_name)
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes", "да"}:
        return True
    if normalized in {"false", "0", "no", "нет"}:
        return False
    return None


def _offer_rating(offer: Offer) -> str | None:
    raw = offer.raw if isinstance(offer.raw, dict) else {}
    value = raw.get("borrower_rating", offer.rating)
    if not value:
        return None
    return str(value).strip().upper()


def _offer_age_group(offer: Offer) -> str | None:
    raw = offer.raw if isinstance(offer.raw, dict) else {}
    value = raw.get("borrower_age_group")
    return str(value).strip() if value else None
