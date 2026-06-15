import asyncio
import logging
from collections import defaultdict
from typing import Any

from app import storage
from app.config import get_settings
from app.models import Offer
from app.user_filters import offer_matches_user_filters, resolved_user_filters

logger = logging.getLogger(__name__)
_CHECK_LOCK: asyncio.Lock | None = None
_CHECK_LOCK_LOOP: asyncio.AbstractEventLoop | None = None


async def check_once(
    bot: Any | None = None,
    include_seen_unnotified: bool = False,
    include_seen_notified: bool = False,
) -> tuple[int, int]:
    async with _get_check_lock():
        return await _check_once_impl(
            bot=bot,
            include_seen_unnotified=include_seen_unnotified,
            include_seen_notified=include_seen_notified,
        )


async def _check_once_impl(
    bot: Any | None = None,
    include_seen_unnotified: bool = False,
    include_seen_notified: bool = False,
) -> tuple[int, int]:
    settings = get_settings()
    offers_count = 0
    notified_count = 0

    try:
        from app.finkit_client import get_offers
        from app.notifier import notify_offer, notify_offer_batch, notify_trial_ended

        expired_subscribers = await storage.aget_expired_subscribers_pending_notice()
        for subscriber in expired_subscribers:
            await notify_trial_ended(
                chat_id=int(subscriber["user_id"]),
                manager_contact=settings.trial_manager_contact,
                bot=bot,
            )
            await storage.amark_trial_ended_notified(int(subscriber["user_id"]))

        active_subscribers, default_threshold, subscriber_filters = await asyncio.to_thread(
            _load_monitoring_context_sync,
            settings.default_score_threshold,
        )
        if not active_subscribers:
            await storage.asave_check_log("ok", offers_count, notified_count)
            logger.info("check skipped because there are no active subscribers")
            return offers_count, notified_count

        offers = await get_offers()
        offers_count = len(offers)
        removed_count, pending_notifications = await asyncio.to_thread(
            _prepare_pending_notifications_sync,
            offers,
            active_subscribers,
            subscriber_filters,
            include_seen_unnotified,
            include_seen_notified,
        )
        if removed_count:
            logger.info("removed stale offers from storage removed_count=%s", removed_count)

        sent_pairs: list[tuple[int, str]] = []
        sent_offer_ids: set[str] = set()

        for subscriber in active_subscribers:
            user_id = int(subscriber["user_id"])
            user_offers = pending_notifications.get(user_id, [])
            if not user_offers:
                continue

            filters = subscriber_filters[user_id]
            threshold = filters.get("borrower_score_min", default_threshold)
            if len(user_offers) > 10:
                await notify_offer_batch(
                    user_offers,
                    threshold,
                    chat_id=user_id,
                    filters=filters,
                    bot=bot,
                )
            else:
                for offer in user_offers:
                    await notify_offer(
                        offer,
                        threshold,
                        chat_id=user_id,
                        filters=filters,
                    bot=bot,
                )

            for offer in user_offers:
                sent_pairs.append((user_id, offer.id))
                sent_offer_ids.add(offer.id)
            notified_count += len(user_offers)

        await asyncio.to_thread(
            _mark_notifications_sent_sync,
            sent_pairs,
            sent_offer_ids,
        )

        await storage.asave_check_log("ok", offers_count, notified_count)
        logger.info(
            "check completed offers_count=%s notified_count=%s subscribers=%s include_seen_unnotified=%s include_seen_notified=%s",
            offers_count,
            notified_count,
            len(active_subscribers),
            include_seen_unnotified,
            include_seen_notified,
        )
        return offers_count, notified_count
    except Exception as exc:
        await storage.asave_check_log("error", offers_count, notified_count, str(exc))
        logger.exception("check failed")
        raise


async def check_after_threshold_change(bot: Any | None = None) -> tuple[int, int]:
    return await check_once(
        bot=bot,
        include_seen_unnotified=True,
        include_seen_notified=True,
    )


def score_matches(score: float | None, threshold: float, compare_mode: str | None = None) -> bool:
    if score is None:
        return False
    mode = (compare_mode or get_settings().score_compare_mode).lower()
    if mode == "gte":
        return score >= threshold
    return score > threshold


def is_available(offer: Offer) -> bool:
    if offer.status is None:
        return True

    status = offer.status.lower().strip()
    if not status or status == "unknown":
        return True
    if any(marker in status for marker in ("закрыто", "недоступно", "closed", "disabled")):
        return False
    if any(marker in status for marker in ("инвестировать", "available", "active")):
        return True
    return True


def _resolved_filters(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _load_monitoring_context_sync(
    default_score_threshold: float,
) -> tuple[list[dict[str, Any]], float, dict[int, dict[str, Any]]]:
    active_subscribers = storage.get_active_subscribers()
    default_threshold = storage.get_threshold(default_score_threshold)
    subscriber_filters = {
        int(subscriber["user_id"]): _resolved_filters(
            resolved_user_filters(
                storage.get_user_filters(int(subscriber["user_id"])),
                default_threshold,
            )
        )
        for subscriber in active_subscribers
    }
    return active_subscribers, default_threshold, subscriber_filters


def _prepare_pending_notifications_sync(
    offers: list[Offer],
    active_subscribers: list[dict[str, Any]],
    subscriber_filters: dict[int, dict[str, Any]],
    include_seen_unnotified: bool,
    include_seen_notified: bool,
) -> tuple[int, dict[int, list[Offer]]]:
    del include_seen_unnotified

    available_offers = [offer for offer in offers if is_available(offer)]
    removed_count = storage.forget_missing_offers({offer.id for offer in available_offers})
    pending_notifications: dict[int, list[Offer]] = defaultdict(list)

    for offer in available_offers:
        storage.save_seen(offer)
        for subscriber in active_subscribers:
            user_id = int(subscriber["user_id"])
            filters = subscriber_filters[user_id]
            if not offer_matches_user_filters(offer, filters):
                continue
            if not include_seen_notified and storage.has_user_offer_notification(user_id, offer.id):
                continue
            pending_notifications[user_id].append(offer)

    return removed_count, pending_notifications


def _mark_notifications_sent_sync(
    sent_pairs: list[tuple[int, str]],
    sent_offer_ids: set[str],
) -> None:
    for user_id, offer_id in sent_pairs:
        storage.mark_user_offer_notified(user_id, offer_id)
    for offer_id in sent_offer_ids:
        storage.mark_notified(offer_id)


def _get_check_lock() -> asyncio.Lock:
    global _CHECK_LOCK, _CHECK_LOCK_LOOP

    loop = asyncio.get_running_loop()
    if _CHECK_LOCK is None or _CHECK_LOCK_LOOP is not loop:
        _CHECK_LOCK = asyncio.Lock()
        _CHECK_LOCK_LOOP = loop
    return _CHECK_LOCK
