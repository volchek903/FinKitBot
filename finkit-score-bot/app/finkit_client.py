import asyncio
import copy
import json
import logging
import math
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from app.config import get_settings
from app.models import Offer
from app.parser import ROW_SELECTOR, parse_offers_from_html, parse_offers_from_json

logger = logging.getLogger(__name__)

PAYLOAD_OFFER_KEYS = {
    "borrower_score",
    "score",
    "score_ball",
    "amount",
    "term",
    "interest_rate",
    "borrower_rating",
    "signed_at",
    "invest",
}
PAGE_KEYS = ("page", "current_page", "currentPage", "pageNumber", "p")
PER_PAGE_KEYS = ("per_page", "perPage", "page_size", "pageSize", "limit")
OFFSET_KEYS = ("offset", "skip")
TOTAL_KEYS = ("total", "total_count", "totalCount", "recordsTotal", "count")
LAST_PAGE_KEYS = ("last_page", "lastPage", "pages", "page_count", "pageCount")


async def get_offers() -> list[Offer]:
    return await get_offers_from_api_or_dom()


async def get_offers_from_api_or_dom() -> list[Offer]:
    from playwright.async_api import async_playwright

    settings = get_settings()
    settings.playwright_state_file.parent.mkdir(parents=True, exist_ok=True)
    context_options: dict[str, Any] = {}
    if settings.playwright_state_file.exists():
        context_options["storage_state"] = str(settings.playwright_state_file)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=settings.playwright_headless)
        context = await browser.new_context(**context_options)
        page = await context.new_page()
        try:
            await page.goto(settings.finkit_offers_url, wait_until="domcontentloaded", timeout=60_000)
            await _login_if_needed(page, context)

            captures = await discover_offers_api(page)
            best_api_offers: list[Offer] = []
            for capture in captures:
                offers = await _offers_from_capture(context, capture)
                if len(offers) > len(best_api_offers):
                    best_api_offers = offers

            if best_api_offers:
                logger.info("offers source=api offers_count=%s", len(best_api_offers))
                return _dedupe_offers(best_api_offers)

            dom_offers = await _collect_offers_from_dom(page)
            logger.info("offers source=dom offers_count=%s", len(dom_offers))
            return dom_offers
        finally:
            await context.close()
            await browser.close()


async def discover_offers_api(page: Any) -> list[dict[str, Any]]:
    settings = get_settings()
    captures: list[dict[str, Any]] = []
    tasks: set[asyncio.Task[None]] = set()

    async def capture_response(response: Any) -> None:
        try:
            content_type = await response.header_value("content-type")
            if settings.finkit_api_base_url not in response.url:
                return
            if "application/json" not in (content_type or "").lower():
                return

            payload = await response.json()
            if not payload_looks_like_offers(payload):
                return

            captures.append(
                {
                    "url": response.url,
                    "method": response.request.method,
                    "post_data": response.request.post_data,
                    "status": response.status,
                    "payload": payload,
                }
            )
        except Exception as exc:
            logger.debug("failed to inspect api response: %s", exc)

    def on_response(response: Any) -> None:
        task = asyncio.create_task(capture_response(response))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    page.on("response", on_response)
    try:
        if page.url == "about:blank":
            await page.goto(settings.finkit_offers_url, wait_until="domcontentloaded", timeout=60_000)
        else:
            await page.reload(wait_until="domcontentloaded", timeout=60_000)
        await _wait_network_idle(page)
        await page.wait_for_timeout(1500)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass

    logger.info("api discovery captures_count=%s", len(captures))
    return captures


def payload_looks_like_offers(payload: Any) -> bool:
    required = {_normalize_key(key) for key in PAYLOAD_OFFER_KEYS}
    for item in _walk_dicts(payload):
        keys = {_normalize_key(key) for key in item.keys()}
        if len(keys & required) >= 2:
            return True
        if "score" in keys and keys & required:
            return True
    return False


async def _login_if_needed(page: Any, context: Any) -> None:
    settings = get_settings()
    if not await _login_appears_required(page):
        await _save_storage_state(context)
        return

    await _raise_if_blocked_auth(page)
    await _open_login_form_if_needed(page)

    login_input = await _first_visible_locator(
        page,
        [
            'input[type="email"]',
            'input[type="text"]',
            'input[name*="login" i]',
            'input[name*="email" i]',
            'input[name*="phone" i]',
        ],
    )
    password_input = await _first_visible_locator(page, ['input[type="password"]'])

    if login_input is None or password_input is None:
        if not await _login_appears_required(page):
            await _save_storage_state(context)
            return
        raise RuntimeError("Требуется авторизация, но поля логина/пароля не найдены")

    if not settings.finkit_login or not settings.finkit_password:
        raise RuntimeError("Требуется авторизация FinKit, но FINKIT_LOGIN/FINKIT_PASSWORD не заполнены")

    await login_input.fill(settings.finkit_login)
    await password_input.fill(settings.finkit_password)

    button = await _first_visible_locator(
        page,
        [
            'button[type="submit"]',
            'button:has-text("Войти")',
            'button:has-text("Продолжить")',
        ],
    )
    if button is None:
        raise RuntimeError("Требуется авторизация, но кнопка входа не найдена")

    await button.click()
    await _wait_network_idle(page)
    await page.wait_for_timeout(1000)
    await _raise_if_blocked_auth(page)

    if await _login_appears_required(page) and not await _page_has_offers_table(page):
        raise RuntimeError("Не удалось авторизоваться в FinKit: проверьте логин и пароль")

    await _save_storage_state(context)


async def _open_login_form_if_needed(page: Any) -> None:
    password_input = await _first_visible_locator(page, ['input[type="password"]'])
    if password_input is not None:
        return

    login_button = await _first_visible_locator(
        page,
        [
            'button:has-text("Войти")',
            'a:has-text("Войти")',
            'button:has-text("Продолжить")',
        ],
    )
    if login_button is not None:
        await login_button.click()
        await page.wait_for_timeout(1000)


async def _login_appears_required(page: Any) -> bool:
    if await _page_has_offers_table(page):
        return False
    password_input = await _first_visible_locator(page, ['input[type="password"]'])
    if password_input is not None:
        return True

    body = await _body_text(page)
    login_markers = ("войти", "авторизация", "логин", "login", "sign in")
    return any(marker in body for marker in login_markers)


async def _raise_if_blocked_auth(page: Any) -> None:
    body = await _body_text(page)
    blockers = (
        "captcha",
        "капча",
        "код подтверждения",
        "подтверждения",
        "2fa",
        "two-factor",
        "sms",
        "смс",
        "одноразовый код",
    )
    if any(marker in body for marker in blockers):
        raise RuntimeError(
            "FinKit запросил капчу, 2FA или код подтверждения. "
            "Бот не обходит защитные механизмы."
        )


async def _collect_offers_from_dom(page: Any) -> list[Offer]:
    settings = get_settings()
    offers: list[Offer] = []
    seen: set[str] = set()
    max_pages = 200

    for page_number in range(1, max_pages + 1):
        try:
            await page.wait_for_selector(ROW_SELECTOR, timeout=15_000)
        except Exception:
            if page_number == 1:
                logger.warning("dom table rows were not found")

        html = await page.content()
        page_offers = parse_offers_from_html(html, settings.finkit_base_url)
        logger.info("offers source=dom page=%s rows=%s", page_number, len(page_offers))
        for offer in page_offers:
            if offer.id in seen:
                continue
            seen.add(offer.id)
            offers.append(offer)

        next_button = await _find_next_button(page)
        if next_button is None:
            break

        signature_before = await _table_signature(page)
        await next_button.click()
        await _wait_network_idle(page)
        await page.wait_for_timeout(750)
        signature_after = await _table_signature(page)
        if signature_after == signature_before:
            logger.info("dom pagination stopped because table did not change")
            break

    return offers


async def _find_next_button(page: Any) -> Any | None:
    selectors = [
        'button[aria-label*="Next"]',
        'button[aria-label*="Следующая"]',
        'button[class*="paginator-next"]',
        '.p-paginator-next',
        '[class*="paginator-next"]',
    ]
    button = await _first_clickable_locator(page, selectors)
    if button is not None:
        return button

    paginator_buttons = page.locator('.p-paginator button, [class*="paginator"] button')
    try:
        count = await paginator_buttons.count()
    except Exception:
        return None

    for index in range(count - 1, -1, -1):
        candidate = paginator_buttons.nth(index)
        if not await _locator_is_clickable(candidate):
            continue
        text = (await _safe_locator_text(candidate)).strip()
        aria = (await candidate.get_attribute("aria-label")) or ""
        classes = (await candidate.get_attribute("class")) or ""
        haystack = f"{text} {aria} {classes}".lower()
        if (
            "next" in haystack
            or "след" in haystack
            or "paginator-next" in haystack
            or not text.isdigit()
        ):
            return candidate
    return None


async def _offers_from_capture(context: Any, capture: dict[str, Any]) -> list[Offer]:
    payloads = await _collect_paginated_payloads(context, capture)
    offers: list[Offer] = []
    for payload in payloads:
        offers.extend(parse_offers_from_json(payload))
    return _dedupe_offers(offers)


async def _collect_paginated_payloads(context: Any, capture: dict[str, Any]) -> list[Any]:
    first_payload = capture["payload"]
    payloads = [first_payload]
    info = _find_pagination_info(first_payload)
    if info is None:
        return payloads

    seen_request_keys = {_request_key(capture["method"], capture["url"], capture.get("post_data"))}

    next_url = info.get("next")
    while next_url:
        absolute_next = urljoin(capture["url"], str(next_url))
        key = _request_key("GET", absolute_next, None)
        if key in seen_request_keys:
            break
        seen_request_keys.add(key)
        payload = await _fetch_json(context, "GET", absolute_next)
        if not payload_looks_like_offers(payload):
            break
        payloads.append(payload)
        next_info = _find_pagination_info(payload)
        next_url = next_info.get("next") if next_info else None

    for method, url, post_data in _build_paginated_requests(capture, info):
        key = _request_key(method, url, post_data)
        if key in seen_request_keys:
            continue
        seen_request_keys.add(key)
        payload = await _fetch_json(context, method, url, post_data)
        if payload_looks_like_offers(payload):
            payloads.append(payload)

    return payloads


async def _fetch_json(context: Any, method: str, url: str, post_data: str | None = None) -> Any:
    try:
        options: dict[str, Any] = {"method": method.upper(), "timeout": 30_000}
        if post_data and method.upper() != "GET":
            options["data"] = post_data
            if post_data.lstrip().startswith(("{", "[")):
                options["headers"] = {"content-type": "application/json"}
        response = await context.request.fetch(url, **options)
        if not response.ok:
            logger.warning("api pagination request failed status=%s url=%s", response.status, url)
            return None
        return await response.json()
    except Exception as exc:
        logger.warning("api pagination request failed url=%s error=%s", url, exc)
        return None


def _build_paginated_requests(
    capture: dict[str, Any],
    info: dict[str, int | str | None],
) -> list[tuple[str, str, str | None]]:
    method = str(capture["method"]).upper()
    url = str(capture["url"])
    post_data = capture.get("post_data")
    requests: list[tuple[str, str, str | None]] = []

    page = _int_or_none(info.get("page"))
    per_page = _int_or_none(info.get("per_page"))
    total = _int_or_none(info.get("total"))
    last_page = _int_or_none(info.get("last_page"))
    offset = _int_or_none(info.get("offset"))
    limit = _int_or_none(info.get("limit")) or per_page

    if page is not None and (last_page is not None or (total and per_page)):
        final_page = last_page or int(math.ceil(total / per_page))
        for next_page in range(page + 1, final_page + 1):
            requests.append(_request_with_page(method, url, post_data, next_page))

    if offset is not None and limit and total and offset + limit < total:
        for next_offset in range(offset + limit, total, limit):
            requests.append(_request_with_offset(method, url, post_data, next_offset, limit))

    return requests


def _request_with_page(
    method: str,
    url: str,
    post_data: str | None,
    page: int,
) -> tuple[str, str, str | None]:
    if method == "GET":
        return method, _url_with_query_value(url, PAGE_KEYS, page, default_key="page"), post_data
    return method, url, _json_body_with_value(post_data, PAGE_KEYS, page, default_key="page")


def _request_with_offset(
    method: str,
    url: str,
    post_data: str | None,
    offset: int,
    limit: int,
) -> tuple[str, str, str | None]:
    if method == "GET":
        paged_url = _url_with_query_value(url, OFFSET_KEYS, offset, default_key="offset")
        paged_url = _url_with_query_value(paged_url, PER_PAGE_KEYS, limit, default_key="limit")
        return method, paged_url, post_data

    body = _json_body_with_value(post_data, OFFSET_KEYS, offset, default_key="offset")
    body = _json_body_with_value(body, PER_PAGE_KEYS, limit, default_key="limit")
    return method, url, body


def _find_pagination_info(payload: Any) -> dict[str, int | str | None] | None:
    for item in _walk_dicts(payload):
        info = {
            "total": _first_int_value(item, TOTAL_KEYS),
            "page": _first_int_value(item, PAGE_KEYS),
            "per_page": _first_int_value(item, PER_PAGE_KEYS),
            "offset": _first_int_value(item, OFFSET_KEYS),
            "limit": _first_int_value(item, PER_PAGE_KEYS),
            "last_page": _first_int_value(item, LAST_PAGE_KEYS),
            "next": _first_next_value(item),
        }
        if info["next"] or (
            info["total"] is not None
            and (
                (info["page"] is not None and (info["per_page"] is not None or info["last_page"] is not None))
                or (info["offset"] is not None and info["limit"] is not None)
            )
        ):
            return info
    return None


def _first_int_value(item: dict[str, Any], aliases: tuple[str, ...]) -> int | None:
    normalized_aliases = {_normalize_key(alias) for alias in aliases}
    for key, value in item.items():
        if _normalize_key(key) in normalized_aliases:
            return _int_or_none(value)
    return None


def _first_next_value(item: dict[str, Any]) -> str | None:
    for key, value in item.items():
        if _normalize_key(key) == "next":
            if isinstance(value, str) and value:
                return value
            if isinstance(value, dict):
                href = value.get("href") or value.get("url")
                if isinstance(href, str) and href:
                    return href
    return None


def _url_with_query_value(url: str, keys: tuple[str, ...], value: int, default_key: str) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    existing_key = next((key for key in query if _normalize_key(key) in {_normalize_key(item) for item in keys}), default_key)
    query[existing_key] = str(value)
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def _json_body_with_value(
    post_data: str | None,
    keys: tuple[str, ...],
    value: int,
    default_key: str,
) -> str | None:
    try:
        body = json.loads(post_data or "{}")
    except json.JSONDecodeError:
        return post_data

    if not isinstance(body, dict):
        return post_data

    updated = copy.deepcopy(body)
    if not _set_nested_value(updated, keys, value):
        updated[default_key] = value
    return json.dumps(updated, ensure_ascii=False)


def _set_nested_value(item: Any, keys: tuple[str, ...], value: int) -> bool:
    normalized_keys = {_normalize_key(key) for key in keys}
    if isinstance(item, dict):
        for key in item:
            if _normalize_key(key) in normalized_keys:
                item[key] = value
                return True
        for nested in item.values():
            if _set_nested_value(nested, keys, value):
                return True
    elif isinstance(item, list):
        for nested in item:
            if _set_nested_value(nested, keys, value):
                return True
    return False


async def _first_visible_locator(page: Any, selectors: list[str]) -> Any | None:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if await locator.count() and await locator.is_visible(timeout=1000):
                return locator
        except Exception:
            continue
    return None


async def _first_clickable_locator(page: Any, selectors: list[str]) -> Any | None:
    for selector in selectors:
        locator = page.locator(selector)
        try:
            count = await locator.count()
        except Exception:
            continue
        for index in range(count):
            candidate = locator.nth(index)
            if await _locator_is_clickable(candidate):
                return candidate
    return None


async def _locator_is_clickable(locator: Any) -> bool:
    try:
        if not await locator.is_visible(timeout=1000):
            return False
        if not await locator.is_enabled(timeout=1000):
            return False
        classes = (await locator.get_attribute("class")) or ""
        aria_disabled = (await locator.get_attribute("aria-disabled")) or ""
        return "p-disabled" not in classes and "disabled" not in classes and aria_disabled.lower() != "true"
    except Exception:
        return False


async def _safe_locator_text(locator: Any) -> str:
    try:
        return await locator.inner_text(timeout=1000)
    except Exception:
        return ""


async def _page_has_offers_table(page: Any) -> bool:
    try:
        return await page.locator(ROW_SELECTOR).count() > 0
    except Exception:
        return False


async def _body_text(page: Any) -> str:
    try:
        return (await page.locator("body").inner_text(timeout=3000)).lower()
    except Exception:
        return ""


async def _wait_network_idle(page: Any) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass


async def _save_storage_state(context: Any) -> None:
    settings = get_settings()
    state_path = Path(settings.playwright_state_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    await context.storage_state(path=str(state_path))


async def _table_signature(page: Any) -> str:
    try:
        rows = await page.locator(ROW_SELECTOR).all_inner_texts()
    except Exception:
        return ""
    if not rows:
        return ""
    return f"{len(rows)}|{rows[0]}|{rows[-1]}"


def _dedupe_offers(offers: list[Offer]) -> list[Offer]:
    seen: set[str] = set()
    result: list[Offer] = []
    for offer in offers:
        if offer.id in seen:
            continue
        seen.add(offer.id)
        result.append(offer)
    return result


def _walk_dicts(payload: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        result.append(payload)
        for value in payload.values():
            result.extend(_walk_dicts(value))
    elif isinstance(payload, list):
        for item in payload:
            result.extend(_walk_dicts(item))
    return result


def _normalize_key(value: str) -> str:
    return re.sub(r"[\s_\-]+", "", str(value).strip().lower())


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _request_key(method: str, url: str, post_data: str | None) -> str:
    return f"{method.upper()} {url} {post_data or ''}"
