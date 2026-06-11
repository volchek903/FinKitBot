from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def main() -> int:
    args = parse_args()
    env_path = find_env_file(args.env)
    if env_path:
        load_env_file(env_path)
        print(f"Loaded env: {env_path}")
    else:
        print("No .env file found, using process environment only.")

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("TELEGRAM_BOT_TOKEN is empty. Put it into .env first.", file=sys.stderr)
        return 2

    bot_info = get_me(token)
    username = bot_info.get("username")
    if username:
        print(f"Token OK. Bot: @{username}")
    else:
        print(f"Token OK. Bot id: {bot_info.get('id', 'unknown')}")

    webhook_info = get_webhook_info(token)
    webhook_url = webhook_info.get("url")
    if webhook_url:
        print(
            "Warning: this bot has a webhook configured. "
            "getUpdates may not receive messages until the webhook is removed."
        )

    deadline = time.monotonic() + args.wait
    offset = None
    printed_chat_ids: set[int] = set()
    attempts = 0

    print("Send any message to your bot now. Waiting for Telegram updates...")
    while True:
        updates = get_updates(token, timeout=args.poll_timeout, offset=offset)
        attempts += 1
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                offset = update_id + 1

            chat = extract_chat(update)
            if not chat:
                continue

            chat_id = chat.get("id")
            if not isinstance(chat_id, int) or chat_id in printed_chat_ids:
                continue

            printed_chat_ids.add(chat_id)
            print_chat(chat, update)

        if printed_chat_ids:
            return 0

        if time.monotonic() >= deadline:
            print(
                "No chat id found. Make sure the bot received a fresh message "
                "and that the token is correct.",
                file=sys.stderr,
            )
            return 1

        print(f"Still waiting... ({attempts})")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Print Telegram chat.id values seen by TELEGRAM_BOT_TOKEN."
    )
    parser.add_argument(
        "--env",
        type=Path,
        default=None,
        help="Path to .env. By default the script searches common project locations.",
    )
    parser.add_argument(
        "--wait",
        type=int,
        default=60,
        help="How many seconds to wait for a message. Default: 60.",
    )
    parser.add_argument(
        "--poll-timeout",
        type=int,
        default=10,
        help="Telegram long-poll timeout in seconds. Default: 10.",
    )
    return parser.parse_args()


def find_env_file(explicit_path: Path | None) -> Path | None:
    if explicit_path:
        return explicit_path if explicit_path.exists() else None

    script_path = Path(__file__).resolve()
    candidates = [
        Path.cwd() / ".env",
        script_path.parents[1] / ".env",
        script_path.parents[2] / ".env",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def load_env_file(path: Path) -> None:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue

        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ[key] = value


def get_updates(token: str, timeout: int, offset: int | None) -> list[dict[str, Any]]:
    query: dict[str, str | int] = {
        "timeout": timeout,
        "allowed_updates": json.dumps(
            ["message", "edited_message", "channel_post", "edited_channel_post"]
        ),
    }
    if offset is not None:
        query["offset"] = offset

    url = f"https://api.telegram.org/bot{token}/getUpdates?{urllib.parse.urlencode(query)}"
    request = urllib.request.Request(url, headers={"User-Agent": "finkit-chat-id-helper"})

    try:
        with urllib.request.urlopen(request, timeout=timeout + 15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Telegram API error {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Cannot reach Telegram API: {exc}") from exc

    if not payload.get("ok"):
        raise SystemExit(f"Telegram API returned error: {payload}")

    result = payload.get("result", [])
    return result if isinstance(result, list) else []


def get_me(token: str) -> dict[str, Any]:
    payload = telegram_api_get(token, "getMe", timeout=15)
    result = payload.get("result")
    if not isinstance(result, dict):
        raise SystemExit(f"Unexpected getMe response: {payload}")
    return result


def get_webhook_info(token: str) -> dict[str, Any]:
    payload = telegram_api_get(token, "getWebhookInfo", timeout=15)
    result = payload.get("result")
    return result if isinstance(result, dict) else {}


def telegram_api_get(token: str, method: str, timeout: int) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    request = urllib.request.Request(url, headers={"User-Agent": "finkit-chat-id-helper"})

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Telegram API error {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Cannot reach Telegram API: {exc}") from exc

    if not payload.get("ok"):
        raise SystemExit(f"Telegram API returned error: {payload}")
    return payload


def extract_chat(update: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("message", "edited_message", "channel_post", "edited_channel_post"):
        message = update.get(key)
        if isinstance(message, dict) and isinstance(message.get("chat"), dict):
            return message["chat"]
    return None


def print_chat(chat: dict[str, Any], update: dict[str, Any]) -> None:
    chat_id = chat["id"]
    chat_type = chat.get("type", "unknown")
    title = chat.get("title") or " ".join(
        part for part in (chat.get("first_name"), chat.get("last_name")) if part
    )
    username = chat.get("username")

    print()
    print(f"TELEGRAM_CHAT_ID={chat_id}")
    print(f"chat type: {chat_type}")
    if title:
        print(f"chat name: {title}")
    if username:
        print(f"username: @{username}")

    message_text = extract_message_text(update)
    if message_text:
        print(f"message: {message_text[:80]}")


def extract_message_text(update: dict[str, Any]) -> str | None:
    for key in ("message", "edited_message", "channel_post", "edited_channel_post"):
        message = update.get(key)
        if isinstance(message, dict):
            text = message.get("text") or message.get("caption")
            if isinstance(text, str):
                return text
    return None


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nStopped by user.")
        raise SystemExit(130)
