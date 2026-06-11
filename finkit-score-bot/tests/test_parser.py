from app.parser import (
    clean_borrower,
    parse_offer_row,
    parse_offers_from_html,
    parse_offers_from_json,
    parse_score,
    stable_offer_id,
)


def test_parse_score() -> None:
    assert parse_score(None) is None
    assert parse_score("65") == 65
    assert parse_score("65,5 балла") == 65.5
    assert parse_score(" score: 70.25 ") == 70.25
    assert parse_score("нет данных") is None


def test_clean_borrower() -> None:
    assert clean_borrower("ДМИТРИЙ Д. i Профиль Просмотрено") == "ДМИТРИЙ Д."
    assert clean_borrower("  ИВАН   И.  ") == "ИВАН И."
    assert clean_borrower(None) is None


def test_parse_offers_from_json() -> None:
    payload = {
        "data": {
            "items": [
                {
                    "offerId": 123,
                    "borrower_score": "66,7",
                    "amount": "1200.00 BYN",
                    "term": 60,
                    "interest_rate": "0.80 %",
                    "borrower_rating": "C",
                    "borrower": "ДМИТРИЙ Д. i Профиль Просмотрено",
                    "signed_at": "11.06.2026",
                    "expected_income": "576.00 BYN",
                    "invest": True,
                }
            ]
        },
        "meta": {"total": 1, "page": 1, "per_page": 30},
    }

    offers = parse_offers_from_json(payload)

    assert len(offers) == 1
    assert offers[0].id == "123"
    assert offers[0].score == 66.7
    assert offers[0].amount == "1200.00 BYN"
    assert offers[0].term == "60"
    assert offers[0].rate == "0.80 %"
    assert offers[0].rating == "C"
    assert offers[0].borrower == "ДМИТРИЙ Д."
    assert offers[0].status == "available"


def test_parse_offer_row() -> None:
    cells = [
        "1200.00 BYN",
        "60",
        "0.80 %",
        "C",
        "45",
        "ДМИТРИЙ Д. i Профиль Просмотрено",
        "11.06.2026",
        "576.00 BYN",
        "Инвестировать",
    ]

    offer = parse_offer_row(cells)

    assert offer is not None
    assert offer.amount == "1200.00 BYN"
    assert offer.term == "60"
    assert offer.rate == "0.80 %"
    assert offer.rating == "C"
    assert offer.score == 45
    assert offer.borrower == "ДМИТРИЙ Д."
    assert offer.signed_at == "11.06.2026"
    assert offer.expected_income == "576.00 BYN"
    assert offer.status == "available"


def test_parse_offers_from_html() -> None:
    html = """
    <table class="p-datatable-table">
      <tbody>
        <tr>
          <td>1200.00 BYN</td>
          <td>60</td>
          <td>0.80 %</td>
          <td>C</td>
          <td>45</td>
          <td>ДМИТРИЙ Д. i Профиль Просмотрено</td>
          <td>11.06.2026</td>
          <td>576.00 BYN</td>
          <td><a href="/app/invest-manually">Инвестировать</a></td>
        </tr>
      </tbody>
    </table>
    """

    offers = parse_offers_from_html(html, "https://finkit.by")

    assert len(offers) == 1
    assert offers[0].url == "https://finkit.by/app/invest-manually"
    assert offers[0].status == "available"


def test_stable_offer_id_generation() -> None:
    first = stable_offer_id(
        score=65,
        amount="100 BYN",
        term="30",
        rate="1 %",
        rating="A",
        borrower="ИВАН И.",
        signed_at="11.06.2026",
        expected_income="10 BYN",
        url="https://finkit.by/app/invest-manually",
    )
    second = stable_offer_id(
        score=65.0,
        amount="100 BYN",
        term="30",
        rate="1 %",
        rating="A",
        borrower="ИВАН И.",
        signed_at="11.06.2026",
        expected_income="10 BYN",
        url="https://finkit.by/app/invest-manually",
    )

    assert first == second
    assert len(first) == 64

