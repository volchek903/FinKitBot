import logging
from typing import Any

from app import storage
from app.config import get_settings
from app.models import Offer

logger = logging.getLogger(__name__)


async def check_once(
    bot: Any | None = None,
    include_seen_unnotified: bool = False,
    include_seen_notified: bool = False,
) -> tuple[int, int]:
    settings = get_settings()
    offers_count = 0
    notified_count = 0

    try:
        from app.finkit_client import get_offers
        from app.notifier import notify_offer, notify_trial_ended

        expired_subscribers = storage.get_expired_subscribers_pending_notice()
        for subscriber in expired_subscribers:
            await notify_trial_ended(
                chat_id=int(subscriber["user_id"]),
                manager_contact=settings.trial_manager_contact,
                bot=bot,
            )
            storage.mark_trial_ended_notified(int(subscriber["user_id"]))

        active_subscribers = storage.get_active_subscribers()
        if not active_subscribers:
            storage.save_check_log("ok", offers_count, notified_count)
            logger.info("check skipped because there are no active subscribers")
            return offers_count, notified_count
        subscriber_thresholds = {
            int(subscriber["user_id"]): _resolve_threshold(
                subscriber.get("score_threshold"),
                settings.default_score_threshold,
            )
            for subscriber in active_subscribers
        }

        offers = await get_offers()
        offers_count = len(offers)

        for offer in offers:
            seen = storage.get_seen(offer.id)
            if seen is None:
                storage.save_seen(offer)

            if not is_available(offer):
                continue

            sent_for_offer = False
            for subscriber in active_subscribers:
                user_id = int(subscriber["user_id"])
                threshold = subscriber_thresholds[user_id]
                if not score_matches(offer.score, threshold):
                    continue
                should_skip = (
                    not include_seen_notified
                    and storage.has_user_offer_notification(user_id, offer.id)
                )
                if should_skip:
                    continue
                if include_seen_unnotified and not include_seen_notified:
                    should_skip = False
                await notify_offer(
                    offer,
                    threshold,
                    chat_id=int(subscriber["user_id"]),
                    bot=bot,
                )
                storage.mark_user_offer_notified(user_id, offer.id)
                notified_count += 1
                sent_for_offer = True

            if sent_for_offer:
                storage.mark_notified(offer.id)

        storage.save_check_log("ok", offers_count, notified_count)
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
        storage.save_check_log("error", offers_count, notified_count, str(exc))
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


def _resolve_threshold(value: object, default: float) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default
