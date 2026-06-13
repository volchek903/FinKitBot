import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.models import Offer


def init_db() -> None:
    path = _database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS offers_seen (
                offer_id TEXT PRIMARY KEY,
                score REAL,
                url TEXT,
                first_seen_at TEXT NOT NULL,
                notified_at TEXT
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS check_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                checked_at TEXT NOT NULL,
                status TEXT NOT NULL,
                offers_count INTEGER DEFAULT 0,
                notified_count INTEGER DEFAULT 0,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS subscribers (
                user_id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                username TEXT,
                first_name TEXT,
                started_at TEXT,
                expires_at TEXT,
                trial_ended_notified_at TEXT
            );

            CREATE TABLE IF NOT EXISTS subscriber_offer_notifications (
                user_id INTEGER NOT NULL,
                offer_id TEXT NOT NULL,
                notified_at TEXT NOT NULL,
                PRIMARY KEY (user_id, offer_id)
            );
            """
        )


def is_seen(offer_id: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM offers_seen WHERE offer_id = ? LIMIT 1",
            (offer_id,),
        ).fetchone()
    return row is not None


def get_seen(offer_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT offer_id, score, url, first_seen_at, notified_at
            FROM offers_seen
            WHERE offer_id = ?
            LIMIT 1
            """,
            (offer_id,),
        ).fetchone()
    return dict(row) if row else None


def save_seen(offer: Offer) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO offers_seen (offer_id, score, url, first_seen_at)
            VALUES (?, ?, ?, ?)
            """,
            (offer.id, offer.score, offer.url, _now()),
        )


def mark_notified(offer_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE offers_seen SET notified_at = ? WHERE offer_id = ?",
            (_now(), offer_id),
        )


def get_subscriber(user_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT user_id, chat_id, username, first_name, started_at, expires_at, trial_ended_notified_at
            FROM subscribers
            WHERE user_id = ?
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def activate_trial(
    user_id: int,
    chat_id: int,
    username: str | None = None,
    first_name: str | None = None,
    duration_hours: int = 24,
) -> dict[str, Any]:
    now = _utcnow()
    current = get_subscriber(user_id)

    if current and current.get("started_at"):
        _upsert_subscriber_identity(user_id, chat_id, username, first_name)
        refreshed = get_subscriber(user_id) or current
        if _is_trial_active_row(refreshed, now=now):
            return {"state": "active", "subscriber": refreshed}
        return {"state": "expired", "subscriber": refreshed}

    expires_at = (now + timedelta(hours=duration_hours)).isoformat(timespec="seconds")
    started_at = now.isoformat(timespec="seconds")
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO subscribers (
                user_id, chat_id, username, first_name, started_at, expires_at, trial_ended_notified_at
            )
            VALUES (?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(user_id) DO UPDATE SET
                chat_id = excluded.chat_id,
                username = excluded.username,
                first_name = excluded.first_name,
                started_at = COALESCE(subscribers.started_at, excluded.started_at),
                expires_at = COALESCE(subscribers.expires_at, excluded.expires_at)
            """,
            (user_id, chat_id, username, first_name, started_at, expires_at),
        )

    subscriber = get_subscriber(user_id)
    return {"state": "started", "subscriber": subscriber}


def get_active_subscribers() -> list[dict[str, Any]]:
    now = _now()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT user_id, chat_id, username, first_name, started_at, expires_at, trial_ended_notified_at
            FROM subscribers
            WHERE started_at IS NOT NULL
              AND expires_at IS NOT NULL
              AND chat_id = user_id
              AND expires_at > ?
            ORDER BY started_at ASC
            """,
            (now,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_expired_subscribers_pending_notice() -> list[dict[str, Any]]:
    now = _now()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT user_id, chat_id, username, first_name, started_at, expires_at, trial_ended_notified_at
            FROM subscribers
            WHERE started_at IS NOT NULL
              AND expires_at IS NOT NULL
              AND chat_id = user_id
              AND expires_at <= ?
              AND trial_ended_notified_at IS NULL
            ORDER BY expires_at ASC
            """,
            (now,),
        ).fetchall()
    return [dict(row) for row in rows]


def mark_trial_ended_notified(user_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE subscribers
            SET trial_ended_notified_at = ?
            WHERE user_id = ?
            """,
            (_now(), user_id),
        )


def has_user_offer_notification(user_id: int, offer_id: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM subscriber_offer_notifications
            WHERE user_id = ? AND offer_id = ?
            LIMIT 1
            """,
            (user_id, offer_id),
        ).fetchone()
    return row is not None


def mark_user_offer_notified(user_id: int, offer_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO subscriber_offer_notifications (user_id, offer_id, notified_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, offer_id) DO UPDATE SET
                notified_at = excluded.notified_at
            """,
            (user_id, offer_id, _now()),
        )


def get_threshold(default: float) -> float:
    with _connect() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            ("score_threshold",),
        ).fetchone()
    if row is None:
        return default
    try:
        return float(row["value"])
    except (TypeError, ValueError):
        return default


def set_threshold(value: float) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            ("score_threshold", str(value)),
        )


def save_check_log(
    status: str,
    offers_count: int = 0,
    notified_count: int = 0,
    error: str | None = None,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO check_logs (checked_at, status, offers_count, notified_count, error)
            VALUES (?, ?, ?, ?, ?)
            """,
            (_now(), status, offers_count, notified_count, error),
        )


def get_recent_offers(limit: int = 5) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT offer_id, score, url, first_seen_at, notified_at
            FROM offers_seen
            ORDER BY first_seen_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_last_check() -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT checked_at, status, offers_count, notified_count, error
            FROM check_logs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    return dict(row) if row else None


def is_trial_active(user_id: int) -> bool:
    subscriber = get_subscriber(user_id)
    if not subscriber:
        return False
    return _is_private_subscriber_row(subscriber) and _is_trial_active_row(subscriber)


def _database_path() -> Path:
    return get_settings().database_file


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_database_path())
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return _utcnow().isoformat(timespec="seconds")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _is_trial_active_row(row: dict[str, Any], now: datetime | None = None) -> bool:
    expires_at = _parse_datetime(row.get("expires_at"))
    if expires_at is None:
        return False
    current = now or _utcnow()
    return expires_at > current


def _is_private_subscriber_row(row: dict[str, Any]) -> bool:
    try:
        return int(row.get("chat_id")) == int(row.get("user_id"))
    except (TypeError, ValueError):
        return False


def _upsert_subscriber_identity(
    user_id: int,
    chat_id: int,
    username: str | None,
    first_name: str | None,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO subscribers (user_id, chat_id, username, first_name)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                chat_id = excluded.chat_id,
                username = excluded.username,
                first_name = excluded.first_name
            """,
            (user_id, chat_id, username, first_name),
        )
