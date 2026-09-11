"""
Booking-code X poster job.

Wired into the scheduler (see app.scheduling.scheduler.job_post_booking_code)
to run every X_POST_INTERVAL_MINUTES. Each tick:

  1. Builds a fresh smart bet-builder slip via the same learned-conviction
     pipeline the manual "smart" endpoint uses (app.bet_builder.smart_builder
     .run_smart_bet), this time with request_code=True so a real SportyBet
     share/booking code is requested for the slip.
  2. Skips quietly (no post, nothing tracked here) when nothing clears the
     learned conviction bar or the average confidence is below
     X_POST_MIN_CONFIDENCE — never forces a pick just to have content, same
     philosophy as run_smart_bet itself.
  3. Skips as a duplicate when the new slip covers mostly the same matches
     as something posted within X_POST_DEDUP_HOURS, so a quiet stretch
     between ticks doesn't repost the same fixtures as a "new" thread.
  4. Composes the thread text with the LLM (app.social.tweet_composer),
     then posts it via app.social.x_client if X posting is configured and
     enabled, otherwise logs it as a dry run.
  5. Records the outcome in the social_posts table either way, so the
     public site can show the latest booking code and the dedup check above
     has something to compare against.
"""
from __future__ import annotations

from typing import Any

from app.config.config import get_settings
from app.storage.social_posts import record_social_post, recent_match_id_sets

import logging

logger = logging.getLogger(__name__)


def run_booking_code_post_job() -> dict[str, Any]:
    from app.bet_builder.smart_builder import run_smart_bet
    from app.social import tweet_composer, x_client

    settings = get_settings()

    try:
        slip = run_smart_bet(
            stake=settings.x_post_stake,
            candidate_limit=50,
            request_code=True,
        )
    except Exception as exc:
        logger.warning("[x_poster] run_smart_bet failed: %s", exc)
        return {"status": "error", "error": str(exc)}

    status = slip.get("status")
    if status != "success":
        # no_candidates / no_smart_bet / synthesis_failed / booking_failed —
        # nothing to post, and nothing was staked or shared. Expected on a
        # quiet day, not an error.
        logger.info("[x_poster] %s, nothing to post", status)
        return {"status": status, "posted": False}

    avg_confidence = int(slip.get("avg_confidence") or 0)
    if avg_confidence < settings.x_post_min_confidence:
        logger.info(
            "[x_poster] avg_confidence=%s below X_POST_MIN_CONFIDENCE=%s, skipping",
            avg_confidence, settings.x_post_min_confidence,
        )
        return {"status": "below_confidence_bar", "posted": False, "avg_confidence": avg_confidence}

    match_ids = sorted({
        str(item.get("match_id") or item.get("sportybet_id") or "")
        for item in (slip.get("selections") or [])
        if item.get("match_id") or item.get("sportybet_id")
    })

    if _is_duplicate(match_ids, dedup_hours=settings.x_post_dedup_hours):
        logger.info("[x_poster] duplicate of a recently posted slip (matches=%s), skipping", match_ids)
        return {"status": "skipped_duplicate", "posted": False, "match_ids": match_ids}

    share_code_result = slip.get("share_code") or {}
    share_code = share_code_result.get("share_code")

    thread = tweet_composer.compose_thread(slip, share_code)

    betbuilder_id = slip.get("betbuilder_id")
    if x_client.is_configured():
        try:
            post_ids = x_client.post_thread(thread)
            record_social_post(
                betbuilder_id=betbuilder_id,
                match_ids=match_ids,
                share_code=share_code,
                thread=thread,
                status="posted",
                dry_run=False,
                post_ids=post_ids,
            )
            logger.info("[x_poster] posted thread (%s tweets) for slip %s", len(post_ids), betbuilder_id)
            return {"status": "posted", "posted": True, "post_ids": post_ids, "match_ids": match_ids}
        except Exception as exc:
            record_social_post(
                betbuilder_id=betbuilder_id,
                match_ids=match_ids,
                share_code=share_code,
                thread=thread,
                status="failed",
                dry_run=False,
                error=str(exc),
            )
            logger.warning("[x_poster] posting failed: %s", exc)
            return {"status": "error", "posted": False, "error": str(exc)}

    record_social_post(
        betbuilder_id=betbuilder_id,
        match_ids=match_ids,
        share_code=share_code,
        thread=thread,
        status="dry_run",
        dry_run=True,
    )
    print(
        f"[x_poster] dry run (X posting not configured/enabled) — composed "
        f"{len(thread)}-tweet thread for slip {betbuilder_id}: {thread[0][:80]!r}..."
    )
    return {"status": "dry_run", "posted": False, "thread": thread, "match_ids": match_ids}


def _is_duplicate(match_ids: list[str], *, dedup_hours: int, overlap_ratio: float = 0.5) -> bool:
    if not match_ids:
        return False
    ids = set(match_ids)
    for recent in recent_match_id_sets(hours=dedup_hours):
        if not recent:
            continue
        overlap = len(ids & recent) / len(ids)
        if overlap >= overlap_ratio:
            return True
    return False
