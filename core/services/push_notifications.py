"""
X Omni -- Web Push delivery.

Sends a browser push notification (reaches the phone even with no tab open)
to every subscription stored for a user, via VAPID-authenticated Web Push.
A dead subscription (404/410) is pruned automatically -- browsers rotate
push endpoints occasionally, and a stale one is not a retryable failure.
"""

from __future__ import annotations

import asyncio
import json
import logging

from pywebpush import WebPushException, webpush

log = logging.getLogger("xomni.push")

MAX_TITLE_CHARS = 120
MAX_BODY_CHARS = 500


def send_push(store, settings, user_id: str, title: str, body: str) -> int:
    """Blocking -- pywebpush uses `requests` under the hood. Call via
    asyncio.to_thread from async code (see send_push_async). Returns the
    count of subscriptions actually delivered to."""
    if not settings.vapid_public_key or not settings.vapid_private_key:
        log.warning("Push requested but VAPID keys are not configured.")
        return 0
    payload = json.dumps({
        "title": str(title or "")[:MAX_TITLE_CHARS],
        "body": str(body or "")[:MAX_BODY_CHARS],
    })
    sent = 0
    for sub in store.list_push_subscriptions(user_id):
        subscription_info = {
            "endpoint": sub["endpoint"],
            "keys": {"p256dh": sub["p256dh_key"], "auth": sub["auth_key"]},
        }
        try:
            webpush(
                subscription_info=subscription_info,
                data=payload,
                vapid_private_key=settings.vapid_private_key,
                vapid_claims={"sub": settings.vapid_subject},
                ttl=3600,
            )
            sent += 1
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):
                store.remove_push_subscription(sub["endpoint"])
            else:
                log.warning("Push delivery failed (%s): %s", status, exc)
    return sent


async def send_push_async(store, settings, user_id: str, title: str, body: str) -> int:
    return await asyncio.to_thread(send_push, store, settings, user_id, title, body)
