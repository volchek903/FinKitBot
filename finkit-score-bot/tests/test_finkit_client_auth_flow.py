import asyncio

from app.config import get_settings
from app.finkit_client import (
    AUTH_POLL_INTERVAL_MS,
    _auth_session_is_authenticated,
    _login_if_needed,
    _wait_for_login_completion,
)


class FakeInput:
    def __init__(self) -> None:
        self.filled: list[str] = []

    async def fill(self, value: str) -> None:
        self.filled.append(value)


class FakeButton:
    def __init__(self, page: "FakePage") -> None:
        self.page = page
        self.clicked = 0

    async def click(self) -> None:
        self.clicked += 1
        self.page.url = "https://finkit.by/app/dashboard"


class FakePage:
    def __init__(self, url: str = "https://finkit.by/login") -> None:
        self.url = url
        self.waits: list[int] = []
        self.open_offers_calls = 0

    async def wait_for_timeout(self, timeout_ms: int) -> None:
        self.waits.append(timeout_ms)


class FakeResponse:
    def __init__(self, payload: object, ok: bool = True) -> None:
        self.payload = payload
        self.ok = ok

    async def json(self) -> object:
        return self.payload


class FakeRequestClient:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    async def fetch(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return self.response


class FakeContext:
    def __init__(self, request_client: FakeRequestClient | None = None) -> None:
        self.request = request_client or FakeRequestClient(FakeResponse({}))
        self.saved = 0


def test_wait_for_login_completion_times_out_when_login_page_stays(monkeypatch) -> None:
    page = FakePage()
    context = FakeContext()

    async def fake_auth_session_is_authenticated(_: FakeContext) -> bool:
        return False

    async def fake_login_appears_required(_: FakePage) -> bool:
        return True

    from app import finkit_client

    monkeypatch.setattr("app.finkit_client.AUTH_COMPLETION_TIMEOUT_MS", 1)
    monkeypatch.setattr("app.finkit_client._auth_session_is_authenticated", fake_auth_session_is_authenticated)
    monkeypatch.setattr("app.finkit_client._login_appears_required", fake_login_appears_required)

    completed = asyncio.run(_wait_for_login_completion(page, context))

    assert completed is False
    assert AUTH_POLL_INTERVAL_MS in page.waits


def test_auth_session_is_authenticated_reads_meta_flag() -> None:
    request_client = FakeRequestClient(
        FakeResponse({"meta": {"is_authenticated": True}, "data": {"user": None}})
    )
    context = FakeContext(request_client=request_client)

    authenticated = asyncio.run(_auth_session_is_authenticated(context))

    assert authenticated is True
    assert request_client.calls
    assert request_client.calls[0]["method"] == "GET"


def test_login_if_needed_waits_for_session_and_reopens_offers_page(monkeypatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("FINKIT_LOGIN", "user@example.com")
    monkeypatch.setenv("FINKIT_PASSWORD", "secret")

    page = FakePage()
    context = FakeContext()
    login_input = FakeInput()
    password_input = FakeInput()
    submit_button = FakeButton(page)
    auth_checks = {"count": 0}

    async def fake_login_appears_required(current_page: FakePage) -> bool:
        return current_page.url.endswith("/login")

    async def fake_open_login_form_if_needed(_: FakePage) -> None:
        return None

    async def fake_raise_if_blocked_auth(_: FakePage) -> None:
        return None

    async def fake_wait_network_idle(_: FakePage) -> None:
        return None

    async def fake_page_has_offers_table(current_page: FakePage) -> bool:
        return current_page.url.endswith("/invest-manually")

    async def fake_save_storage_state(current_context: FakeContext) -> None:
        current_context.saved += 1

    async def fake_auth_session_is_authenticated(_: FakeContext) -> bool:
        auth_checks["count"] += 1
        return auth_checks["count"] >= 2

    async def fake_open_offers_page(current_page: FakePage) -> None:
        current_page.open_offers_calls += 1
        current_page.url = "https://finkit.by/app/invest-manually"

    async def fake_first_visible_locator(_: FakePage, selectors: list[str]):
        if selectors == ['input[type="password"]']:
            return password_input
        if selectors == [
            'input[type="email"]',
            'input[type="text"]',
            'input[name*="login" i]',
            'input[name*="email" i]',
            'input[name*="phone" i]',
        ]:
            return login_input
        if selectors == [
            'button[type="submit"]',
            'button:has-text("Войти")',
            'button:has-text("Продолжить")',
        ]:
            return submit_button
        raise AssertionError(f"Unexpected selectors: {selectors}")

    monkeypatch.setattr("app.finkit_client._login_appears_required", fake_login_appears_required)
    monkeypatch.setattr("app.finkit_client._open_login_form_if_needed", fake_open_login_form_if_needed)
    monkeypatch.setattr("app.finkit_client._raise_if_blocked_auth", fake_raise_if_blocked_auth)
    monkeypatch.setattr("app.finkit_client._wait_network_idle", fake_wait_network_idle)
    monkeypatch.setattr("app.finkit_client._page_has_offers_table", fake_page_has_offers_table)
    monkeypatch.setattr("app.finkit_client._save_storage_state", fake_save_storage_state)
    monkeypatch.setattr("app.finkit_client._auth_session_is_authenticated", fake_auth_session_is_authenticated)
    monkeypatch.setattr("app.finkit_client._open_offers_page", fake_open_offers_page)
    monkeypatch.setattr("app.finkit_client._first_visible_locator", fake_first_visible_locator)

    asyncio.run(_login_if_needed(page, context))

    assert login_input.filled == ["user@example.com"]
    assert password_input.filled == ["secret"]
    assert submit_button.clicked == 1
    assert auth_checks["count"] >= 2
    assert page.open_offers_calls == 1
    assert page.url == "https://finkit.by/app/invest-manually"
    assert context.saved == 1
