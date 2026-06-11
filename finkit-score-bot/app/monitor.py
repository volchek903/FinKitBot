import logging

from app import storage
from app.config import get_settings
from app.models import Offer

logger = logging.getLogger(__name__)


async def check_once(include_seen_unnotified: bool = False) -> tuple[int, int]:
    settings = get_settings()
    threshold = storage.get_threshold(settings.default_score_threshold)
    offers_count = 0
    notified_count = 0

    try:
        from app.finkit_client import get_offers
        from app.notifier import notify_offer

        offers = await get_offers()
        offers_count = len(offers)

        for offer in offers:
            seen = storage.get_seen(offer.id)
            if seen is not None:
                if not include_seen_unnotified or seen.get("notified_at"):
                    continue
            else:
                storage.save_seen(offer)

            if score_matches(offer.score, threshold) and is_available(offer):
                await notify_offer(offer, threshold)
                storage.mark_notified(offer.id)
                notified_count += 1

        storage.save_check_log("ok", offers_count, notified_count)
        logger.info(
            "check completed offers_count=%s notified_count=%s threshold=%s include_seen_unnotified=%s",
            offers_count,
            notified_count,
            threshold,
            include_seen_unnotified,
        )
        return offers_count, notified_count
    except Exception as exc:
        storage.save_check_log("error", offers_count, notified_count, str(exc))
        logger.exception("check failed")
        raise


async def check_after_threshold_change() -> tuple[int, int]:
    return await check_once(include_seen_unnotified=True)


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
