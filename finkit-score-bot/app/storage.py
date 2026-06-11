import sqlite3
from datetime import datetime, timezone
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


def _database_path() -> Path:
    return get_settings().database_file


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_database_path())
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
