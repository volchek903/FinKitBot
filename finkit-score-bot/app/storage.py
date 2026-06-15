import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.models import Offer
from app.user_filters import empty_user_filters

SQLITE_CONNECT_TIMEOUT_SECONDS = 30
SQLITE_BUSY_TIMEOUT_MS = 30_000


def init_db() -> None:
    path = _database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
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
                created_at TEXT,
                score_threshold REAL,
                filters_json TEXT,
                search_enabled INTEGER,
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
        _ensure_column(conn, "subscribers", "score_threshold", "REAL")
        _ensure_column(conn, "subscribers", "filters_json", "TEXT")
        _ensure_column(conn, "subscribers", "search_enabled", "INTEGER")
        _ensure_column(conn, "subscribers", "created_at", "TEXT")
        conn.execute(
            """
            UPDATE subscribers
            SET created_at = COALESCE(created_at, started_at, expires_at, trial_ended_notified_at, ?)
            WHERE created_at IS NULL
            """,
            (_now(),),
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
            INSERT INTO offers_seen (offer_id, score, url, first_seen_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(offer_id) DO UPDATE SET
                score = excluded.score,
                url = excluded.url
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
            SELECT
                user_id,
                chat_id,
                username,
                first_name,
                created_at,
                score_threshold,
                filters_json,
                search_enabled,
                started_at,
                expires_at,
                trial_ended_notified_at
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
        set_user_search_enabled(user_id, enabled=True)
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
                user_id,
                chat_id,
                username,
                first_name,
                created_at,
                search_enabled,
                started_at,
                expires_at,
                trial_ended_notified_at
            )
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, NULL)
            ON CONFLICT(user_id) DO UPDATE SET
                chat_id = excluded.chat_id,
                username = excluded.username,
                first_name = excluded.first_name,
                created_at = COALESCE(subscribers.created_at, excluded.created_at),
                search_enabled = 1,
                started_at = COALESCE(subscribers.started_at, excluded.started_at),
                expires_at = COALESCE(subscribers.expires_at, excluded.expires_at)
            """,
            (user_id, chat_id, username, first_name, started_at, started_at, expires_at),
        )

    subscriber = get_subscriber(user_id)
    return {"state": "started", "subscriber": subscriber}


def get_active_subscribers() -> list[dict[str, Any]]:
    now = _now()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT
                user_id,
                chat_id,
                username,
                first_name,
                created_at,
                score_threshold,
                filters_json,
                search_enabled,
                started_at,
                expires_at,
                trial_ended_notified_at
            FROM subscribers
            WHERE started_at IS NOT NULL
              AND expires_at IS NOT NULL
              AND chat_id = user_id
              AND COALESCE(search_enabled, 1) = 1
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
            SELECT
                user_id,
                chat_id,
                username,
                first_name,
                created_at,
                score_threshold,
                filters_json,
                search_enabled,
                started_at,
                expires_at,
                trial_ended_notified_at
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


def forget_missing_offers(current_offer_ids: set[str]) -> int:
    with _connect() as conn:
        if current_offer_ids:
            placeholders = ", ".join("?" for _ in current_offer_ids)
            params = tuple(current_offer_ids)
            row = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM offers_seen
                WHERE offer_id NOT IN ({placeholders})
                """,
                params,
            ).fetchone()
            conn.execute(
                f"""
                DELETE FROM subscriber_offer_notifications
                WHERE offer_id NOT IN ({placeholders})
                """,
                params,
            )
            conn.execute(
                f"""
                DELETE FROM offers_seen
                WHERE offer_id NOT IN ({placeholders})
                """,
                params,
            )
        else:
            row = conn.execute("SELECT COUNT(*) FROM offers_seen").fetchone()
            conn.execute("DELETE FROM subscriber_offer_notifications")
            conn.execute("DELETE FROM offers_seen")
    return int(row[0]) if row else 0


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


def get_user_threshold(user_id: int, default: float) -> float:
    filters = get_user_filters(user_id)
    value = filters.get("borrower_score_min")
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def get_user_filters(user_id: int) -> dict[str, Any]:
    subscriber = get_subscriber(user_id)
    if not subscriber:
        return empty_user_filters()

    filters_json = subscriber.get("filters_json")
    filters = _parse_filters(filters_json)
    if "borrower_score_min" not in filters and subscriber.get("score_threshold") is not None:
        filters["borrower_score_min"] = subscriber.get("score_threshold")
    return filters


def set_user_filter(
    user_id: int,
    chat_id: int,
    key: str,
    value: Any,
    username: str | None = None,
    first_name: str | None = None,
) -> dict[str, Any]:
    filters = get_user_filters(user_id)
    if value is None:
        filters.pop(key, None)
    else:
        filters[key] = value
    _save_user_filters(
        user_id=user_id,
        chat_id=chat_id,
        filters=filters,
        username=username,
        first_name=first_name,
    )
    return filters


def set_user_filters(
    user_id: int,
    chat_id: int,
    filters: dict[str, Any],
    username: str | None = None,
    first_name: str | None = None,
) -> dict[str, Any]:
    _save_user_filters(
        user_id=user_id,
        chat_id=chat_id,
        filters=dict(filters),
        username=username,
        first_name=first_name,
    )
    return get_user_filters(user_id)


def set_user_threshold(
    user_id: int,
    chat_id: int,
    value: float,
    username: str | None = None,
    first_name: str | None = None,
) -> None:
    set_user_filter(
        user_id=user_id,
        chat_id=chat_id,
        key="borrower_score_min",
        value=value,
        username=username,
        first_name=first_name,
    )


def set_user_search_enabled(user_id: int, enabled: bool) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE subscribers
            SET search_enabled = ?
            WHERE user_id = ?
            """,
            (1 if enabled else 0, user_id),
        )


def pause_user_search(user_id: int) -> bool:
    subscriber = get_subscriber(user_id)
    if not subscriber:
        return False
    was_enabled = _search_enabled_value(subscriber)
    if was_enabled:
        set_user_search_enabled(user_id, enabled=False)
    return was_enabled


def is_user_search_enabled(user_id: int) -> bool:
    subscriber = get_subscriber(user_id)
    if not subscriber:
        return False
    return _search_enabled_value(subscriber)


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


def get_private_user_ids() -> list[int]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT user_id
            FROM subscribers
            WHERE chat_id = user_id
            ORDER BY COALESCE(created_at, started_at, expires_at) ASC, user_id ASC
            """
        ).fetchall()
    return [int(row["user_id"]) for row in rows]


def get_subscribers_page(limit: int = 10, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    safe_limit = max(1, limit)
    safe_offset = max(0, offset)
    with _connect() as conn:
        total_row = conn.execute(
            """
            SELECT COUNT(*) AS total_count
            FROM subscribers
            WHERE chat_id = user_id
            """
        ).fetchone()
        rows = conn.execute(
            """
            SELECT
                user_id,
                chat_id,
                username,
                first_name,
                created_at,
                score_threshold,
                filters_json,
                search_enabled,
                started_at,
                expires_at,
                trial_ended_notified_at
            FROM subscribers
            WHERE chat_id = user_id
            ORDER BY COALESCE(created_at, started_at, expires_at) DESC, user_id DESC
            LIMIT ? OFFSET ?
            """,
            (safe_limit, safe_offset),
        ).fetchall()
    total_count = int(total_row["total_count"]) if total_row else 0
    return [dict(row) for row in rows], total_count


def get_user_stats() -> dict[str, Any]:
    now = _utcnow()
    now_text = now.isoformat(timespec="seconds")
    day_ago = (now - timedelta(days=1)).isoformat(timespec="seconds")
    week_ago = (now - timedelta(days=7)).isoformat(timespec="seconds")
    month_ago = (now - timedelta(days=30)).isoformat(timespec="seconds")

    with _connect() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total_users,
                SUM(CASE WHEN filters_json IS NOT NULL THEN 1 ELSE 0 END) AS configured_users,
                SUM(CASE WHEN started_at IS NOT NULL THEN 1 ELSE 0 END) AS activated_users,
                SUM(CASE WHEN COALESCE(created_at, started_at) >= ? THEN 1 ELSE 0 END) AS registrations_day,
                SUM(CASE WHEN COALESCE(created_at, started_at) >= ? THEN 1 ELSE 0 END) AS registrations_week,
                SUM(CASE WHEN COALESCE(created_at, started_at) >= ? THEN 1 ELSE 0 END) AS registrations_month,
                SUM(
                    CASE
                        WHEN started_at IS NOT NULL
                             AND expires_at IS NOT NULL
                             AND expires_at > ?
                        THEN 1 ELSE 0
                    END
                ) AS active_users,
                SUM(
                    CASE
                        WHEN started_at IS NOT NULL
                             AND expires_at IS NOT NULL
                             AND expires_at > ?
                             AND COALESCE(search_enabled, 1) = 1
                        THEN 1 ELSE 0
                    END
                ) AS running_users,
                SUM(
                    CASE
                        WHEN started_at IS NOT NULL
                             AND expires_at IS NOT NULL
                             AND expires_at > ?
                             AND COALESCE(search_enabled, 1) = 0
                        THEN 1 ELSE 0
                    END
                ) AS paused_users,
                SUM(
                    CASE
                        WHEN started_at IS NOT NULL
                             AND expires_at IS NOT NULL
                             AND expires_at <= ?
                        THEN 1 ELSE 0
                    END
                ) AS expired_users,
                MAX(COALESCE(created_at, started_at)) AS last_registration_at,
                MAX(started_at) AS last_activation_at
            FROM subscribers
            WHERE chat_id = user_id
            """,
            (day_ago, week_ago, month_ago, now_text, now_text, now_text, now_text),
        ).fetchone()
    return dict(row) if row else {}


def is_trial_active(user_id: int) -> bool:
    subscriber = get_subscriber(user_id)
    if not subscriber:
        return False
    return _is_private_subscriber_row(subscriber) and _is_trial_active_row(subscriber)


def is_trial_running(user_id: int) -> bool:
    subscriber = get_subscriber(user_id)
    if not subscriber:
        return False
    return (
        _is_private_subscriber_row(subscriber)
        and _is_trial_active_row(subscriber)
        and _search_enabled_value(subscriber)
    )


def _database_path() -> Path:
    return get_settings().database_file


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        _database_path(),
        timeout=SQLITE_CONNECT_TIMEOUT_SECONDS,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
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


def _search_enabled_value(row: dict[str, Any]) -> bool:
    value = row.get("search_enabled")
    if value is None:
        return True
    try:
        return bool(int(value))
    except (TypeError, ValueError):
        return bool(value)


def _upsert_subscriber_identity(
    user_id: int,
    chat_id: int,
    username: str | None,
    first_name: str | None,
) -> None:
    created_at = _now()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO subscribers (user_id, chat_id, username, first_name, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                chat_id = excluded.chat_id,
                username = excluded.username,
                first_name = excluded.first_name,
                created_at = COALESCE(subscribers.created_at, excluded.created_at)
            """,
            (user_id, chat_id, username, first_name, created_at),
        )


def _save_user_filters(
    *,
    user_id: int,
    chat_id: int,
    filters: dict[str, Any],
    username: str | None,
    first_name: str | None,
) -> None:
    threshold = filters.get("borrower_score_min")
    created_at = _now()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO subscribers (
                user_id,
                chat_id,
                username,
                first_name,
                created_at,
                score_threshold,
                filters_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                chat_id = excluded.chat_id,
                username = excluded.username,
                first_name = excluded.first_name,
                created_at = COALESCE(subscribers.created_at, excluded.created_at),
                score_threshold = excluded.score_threshold,
                filters_json = excluded.filters_json
            """,
            (
                user_id,
                chat_id,
                username,
                first_name,
                created_at,
                threshold,
                json.dumps(filters, ensure_ascii=False, sort_keys=True),
            ),
        )


def _ensure_column(
    conn: sqlite3.Connection,
    table_name: str,
    column_name: str,
    column_definition: str,
) -> None:
    columns = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    if any(column["name"] == column_name for column in columns):
        return
    conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")


def _parse_filters(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def ainit_db() -> None:
    await asyncio.to_thread(init_db)


async def aget_subscriber(user_id: int) -> dict[str, Any] | None:
    return await asyncio.to_thread(get_subscriber, user_id)


async def aactivate_trial(
    user_id: int,
    chat_id: int,
    username: str | None = None,
    first_name: str | None = None,
    duration_hours: int = 24,
) -> dict[str, Any]:
    return await asyncio.to_thread(
        activate_trial,
        user_id,
        chat_id,
        username,
        first_name,
        duration_hours,
    )


async def aget_active_subscribers() -> list[dict[str, Any]]:
    return await asyncio.to_thread(get_active_subscribers)


async def aget_expired_subscribers_pending_notice() -> list[dict[str, Any]]:
    return await asyncio.to_thread(get_expired_subscribers_pending_notice)


async def amark_trial_ended_notified(user_id: int) -> None:
    await asyncio.to_thread(mark_trial_ended_notified, user_id)


async def aforget_missing_offers(current_offer_ids: set[str]) -> int:
    return await asyncio.to_thread(forget_missing_offers, current_offer_ids)


async def aget_threshold(default: float) -> float:
    return await asyncio.to_thread(get_threshold, default)


async def aget_user_filters(user_id: int) -> dict[str, Any]:
    return await asyncio.to_thread(get_user_filters, user_id)


async def aset_user_filter(
    user_id: int,
    chat_id: int,
    key: str,
    value: Any,
    username: str | None = None,
    first_name: str | None = None,
) -> dict[str, Any]:
    return await asyncio.to_thread(
        set_user_filter,
        user_id,
        chat_id,
        key,
        value,
        username,
        first_name,
    )


async def aset_user_filters(
    user_id: int,
    chat_id: int,
    filters: dict[str, Any],
    username: str | None = None,
    first_name: str | None = None,
) -> dict[str, Any]:
    return await asyncio.to_thread(
        set_user_filters,
        user_id,
        chat_id,
        filters,
        username,
        first_name,
    )


async def apause_user_search(user_id: int) -> bool:
    return await asyncio.to_thread(pause_user_search, user_id)


async def ais_trial_active(user_id: int) -> bool:
    return await asyncio.to_thread(is_trial_active, user_id)


async def ais_trial_running(user_id: int) -> bool:
    return await asyncio.to_thread(is_trial_running, user_id)


async def aset_threshold(value: float) -> None:
    await asyncio.to_thread(set_threshold, value)


async def asave_check_log(
    status: str,
    offers_count: int = 0,
    notified_count: int = 0,
    error: str | None = None,
) -> None:
    await asyncio.to_thread(save_check_log, status, offers_count, notified_count, error)


async def aget_recent_offers(limit: int = 5) -> list[dict[str, Any]]:
    return await asyncio.to_thread(get_recent_offers, limit)


async def aget_last_check() -> dict[str, Any] | None:
    return await asyncio.to_thread(get_last_check)


async def aget_private_user_ids() -> list[int]:
    return await asyncio.to_thread(get_private_user_ids)


async def aget_subscribers_page(
    limit: int = 10,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    return await asyncio.to_thread(get_subscribers_page, limit, offset)


async def aget_user_stats() -> dict[str, Any]:
    return await asyncio.to_thread(get_user_stats)
