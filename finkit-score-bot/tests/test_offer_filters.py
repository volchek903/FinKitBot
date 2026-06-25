import json
import sys
import types
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

config_stub = types.ModuleType("app.config")
config_stub.get_settings = lambda: None
sys.modules.setdefault("app.config", config_stub)

from app.offers_url import build_offers_url, strip_user_filter_query
from app.parser import parse_offers_from_json
from app.user_filters import offer_matches_user_filters, resolved_user_filters

DATA_DIR = ROOT / "data"
RATING_ORDER = ("A", "B", "C", "D", "E", "F")


def _load_payload(filename: str) -> dict:
    return json.loads((DATA_DIR / filename).read_text())["payload"]


def _iter_offer_dicts(node):
    if isinstance(node, dict):
        if {"id", "borrower_score", "amount", "term", "interest_rate", "borrower_rating"} <= set(node):
            yield node
        for value in node.values():
            yield from _iter_offer_dicts(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_offer_dicts(item)


def _float_value(value) -> float:
    return float(str(value))


class OfferFiltersTest(unittest.TestCase):
    def test_real_payload_filters_match_expected_offers(self) -> None:
        cases = (
            ("score_min_30", {"borrower_score_min": 30}, lambda item: _float_value(item["borrower_score"]) >= 30),
            (
                "amount_500_1000",
                {"amount_min": 500, "amount_max": 1000},
                lambda item: 500 <= _float_value(item["amount"]) <= 1000,
            ),
            (
                "term_30_45",
                {"term_min": 30, "term_max": 45},
                lambda item: 30 <= _float_value(item["term"]) <= 45,
            ),
            (
                "rate_max_1",
                {"interest_rate_max": 1},
                lambda item: _float_value(item["interest_rate"]) <= 1,
            ),
            (
                "rating_c_to_d",
                {"borrower_rating_min": "C", "borrower_rating_max": "D"},
                lambda item: RATING_ORDER.index(str(item["borrower_rating"]).upper()) in {2, 3},
            ),
            (
                "income_confirmed",
                {"borrower_income_confirmed": True},
                lambda item: bool(item["borrower_income_confirmed"]) is True,
            ),
            (
                "enforcement_absent",
                {"borrower_enforcement_up_to_1_month_absent": True},
                lambda item: bool(item["borrower_has_enforcement_proceedings"]) is False,
            ),
            (
                "age_28_35_or_36_49",
                {"borrower_age_group": ["28-35", "36-49"]},
                lambda item: str(item["borrower_age_group"]) in {"28-35", "36-49"},
            ),
        )

        for capture_name in ("capture_9.json", "capture_12.json"):
            payload = _load_payload(capture_name)
            raw_offers = list(_iter_offer_dicts(payload))
            parsed_offers = parse_offers_from_json(payload)
            self.assertEqual(len({item["id"] for item in raw_offers}), len(parsed_offers))

            for case_name, filters, predicate in cases:
                with self.subTest(capture=capture_name, case=case_name):
                    expected_ids = {str(item["id"]) for item in raw_offers if predicate(item)}
                    actual_ids = {
                        offer.id for offer in parsed_offers if offer_matches_user_filters(offer, filters)
                    }
                    self.assertSetEqual(expected_ids, actual_ids)

    def test_legacy_invest_filters_are_ignored(self) -> None:
        payload = _load_payload("capture_12.json")
        offer = parse_offers_from_json(payload)[0]

        resolved = resolved_user_filters({"invest_min": 100, "invest_max": 500}, default_threshold=0)
        self.assertNotIn("invest_min", resolved)
        self.assertNotIn("invest_max", resolved)
        self.assertTrue(offer_matches_user_filters(offer, {"invest_min": 100, "invest_max": 500}))

    def test_legacy_invest_query_params_are_removed_from_links(self) -> None:
        base_url = (
            "https://finkit.by/app/invest-manually"
            "?invest_min=100&invest_max=500&borrower_score_min=15&page=1"
        )

        stripped_url = strip_user_filter_query(base_url)
        stripped_query = parse_qs(urlsplit(stripped_url).query)
        self.assertNotIn("invest_min", stripped_query)
        self.assertNotIn("invest_max", stripped_query)
        self.assertNotIn("borrower_score_min", stripped_query)
        self.assertEqual(stripped_query.get("page"), ["1"])

        rebuilt_url = build_offers_url(
            base_url,
            threshold=30,
            filters={"borrower_score_min": 45, "borrower_age_group": ["28-35"]},
        )
        rebuilt_query = parse_qs(urlsplit(rebuilt_url).query)
        self.assertNotIn("invest_min", rebuilt_query)
        self.assertNotIn("invest_max", rebuilt_query)
        self.assertEqual(rebuilt_query.get("page"), ["1"])
        self.assertEqual(rebuilt_query.get("borrower_score_min"), ["45"])
        self.assertEqual(rebuilt_query.get("borrower_age_group"), ["28-35"])


if __name__ == "__main__":
    unittest.main()
