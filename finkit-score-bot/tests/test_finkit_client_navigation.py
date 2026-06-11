import asyncio
from typing import Any

from app.finkit_client import (
    NAVIGATION_TIMEOUT_MS,
    NETWORK_IDLE_TIMEOUT_MS,
    POST_NAVIGATION_DELAY_MS,
    _capture_priority,
    discover_offers_api,
    get_settings,
    _open_offers_page,
)


class FakePage:
    def __init__(self, url: str = "about:blank", fail_network_idle: bool = False) -> None:
        self.url = url
        self.fail_network_idle = fail_network_idle
        self.goto_calls: list[dict[str, Any]] = []
        self.reload_calls: list[dict[str, Any]] = []
        self.wait_for_load_state_calls: list[dict[str, Any]] = []
        self.wait_for_timeout_calls: list[int] = []
        self.listeners: dict[str, Any] = {}

    async def goto(self, url: str, wait_until: str, timeout: int) -> None:
        self.goto_calls.append({"url": url, "wait_until": wait_until, "timeout": timeout})
        self.url = url

    async def reload(self, wait_until: str, timeout: int) -> None:
        self.reload_calls.append({"wait_until": wait_until, "timeout": timeout})

    async def wait_for_load_state(self, state: str, timeout: int) -> None:
        self.wait_for_load_state_calls.append({"state": state, "timeout": timeout})
        if self.fail_network_idle:
            raise RuntimeError("network is still busy")

    async def wait_for_timeout(self, timeout_ms: int) -> None:
        self.wait_for_timeout_calls.append(timeout_ms)

    def on(self, event: str, callback: Any) -> None:
        self.listeners[event] = callback

    def remove_listener(self, event: str, callback: Any) -> None:
        if self.listeners.get(event) is callback:
            del self.listeners[event]


def test_open_offers_page_uses_commit_navigation() -> None:
    get_settings.cache_clear()
    settings = get_settings()
    page = FakePage()

    asyncio.run(_open_offers_page(page))

    assert page.goto_calls == [
        {
            "url": settings.finkit_offers_url,
            "wait_until": "commit",
            "timeout": NAVIGATION_TIMEOUT_MS,
        }
    ]
    assert page.wait_for_load_state_calls == [
        {"state": "networkidle", "timeout": NETWORK_IDLE_TIMEOUT_MS}
    ]
    assert page.wait_for_timeout_calls == [POST_NAVIGATION_DELAY_MS]


def test_open_offers_page_tolerates_network_idle_timeout() -> None:
    page = FakePage(fail_network_idle=True)

    asyncio.run(_open_offers_page(page))

    assert page.goto_calls
    assert page.wait_for_timeout_calls == [POST_NAVIGATION_DELAY_MS]


def test_discover_offers_api_reloads_with_commit_navigation() -> None:
    page = FakePage(url="https://finkit.by/app/invest-manually")

    captures = asyncio.run(discover_offers_api(page))

    assert captures == []
    assert page.reload_calls == [
        {"wait_until": "commit", "timeout": NAVIGATION_TIMEOUT_MS}
    ]


def test_capture_priority_prefers_filtered_loans_endpoint() -> None:
    target_url = "https://finkit.by/app/invest-manually?borrower_score_min=30"
    unfiltered_capture = {
        "url": "https://api-p2p.finkit.by/loans-to-invest/?page=1&ordering=-signed_at",
        "payload": {"results": []},
    }
    filtered_capture = {
        "url": "https://api-p2p.finkit.by/loans-to-invest/?borrower_score_min=30&page=1&ordering=-signed_at",
        "payload": {"results": []},
    }

    assert _capture_priority(filtered_capture, target_url) > _capture_priority(
        unfiltered_capture,
        target_url,
    )
