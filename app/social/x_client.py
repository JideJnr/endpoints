"""
Minimal X (Twitter) API v2 client for posting a booking-code thread.

Deliberately thin: one function (`post_thread`) that posts a list of tweet
texts as a thread (each reply chained to the previous tweet's id via
`reply.in_reply_to_tweet_id`), using OAuth1.0a user-context signing — the
auth mode X's v2 POST /2/tweets endpoint requires for posting on behalf of
a single developer-owned account (the standard setup for a bot account).

Credentials come from Settings (X_API_KEY / X_API_SECRET / X_ACCESS_TOKEN /
X_ACCESS_TOKEN_SECRET). When they aren't all present, or X_POSTING_ENABLED
is false, callers should use `is_configured()` to skip straight to a
dry-run — this module never silently no-ops a post; it raises if asked to
post without credentials, so a misconfiguration is loud, not swallowed.
"""
from __future__ import annotations

from typing import Any

from app.config.config import get_settings

_TWEETS_URL = "https://api.x.com/2/tweets"


def is_configured() -> bool:
    settings = get_settings()
    return bool(
        settings.x_posting_enabled
        and settings.x_api_key
        and settings.x_api_secret
        and settings.x_access_token
        and settings.x_access_token_secret
    )


def post_thread(texts: list[str], *, timeout: int = 20) -> list[str]:
    """Post `texts` as a thread. Returns the list of created tweet ids, in
    order. Raises RuntimeError on the first failed tweet — a partially
    posted thread is left as-is (the ids posted so far are on the account
    regardless), the caller records what actually went out via the
    returned ids up to the exception.
    """
    if not texts:
        raise ValueError("post_thread requires at least one tweet")
    settings = get_settings()
    if not is_configured():
        raise RuntimeError(
            "X posting is not configured — set X_POSTING_ENABLED=true and "
            "X_API_KEY/X_API_SECRET/X_ACCESS_TOKEN/X_ACCESS_TOKEN_SECRET"
        )

    from requests_oauthlib import OAuth1Session

    session = OAuth1Session(
        client_key=settings.x_api_key,
        client_secret=settings.x_api_secret,
        resource_owner_key=settings.x_access_token,
        resource_owner_secret=settings.x_access_token_secret,
    )

    post_ids: list[str] = []
    previous_id: str | None = None
    try:
        for text in texts:
            body: dict[str, Any] = {"text": text}
            if previous_id:
                body["reply"] = {"in_reply_to_tweet_id": previous_id}
            response = session.post(_TWEETS_URL, json=body, timeout=timeout)
            if response.status_code >= 300:
                raise RuntimeError(
                    f"X API returned {response.status_code} posting tweet "
                    f"{len(post_ids) + 1}/{len(texts)}: {response.text[:500]}"
                )
            data = response.json()
            tweet_id = str((data.get("data") or {}).get("id") or "")
            if not tweet_id:
                raise RuntimeError(f"X API did not return a tweet id: {response.text[:500]}")
            post_ids.append(tweet_id)
            previous_id = tweet_id
    finally:
        session.close()

    return post_ids
