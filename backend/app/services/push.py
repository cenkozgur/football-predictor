"""Web Push notification helpers.

VAPID config
------------
Three env vars drive this:
    VAPID_PRIVATE_KEY  — PEM-encoded EC private key, generated once
    VAPID_PUBLIC_KEY   — base64url-encoded EC public key, given to browsers
    VAPID_SUBJECT      — mailto:owner@example.com or app URL

When any of them is missing, push delivery is silently a no-op. This
keeps the rest of the pipeline (resolver, recorder) running fine on
local dev or before keys are wired up — push is a side effect, not a
required step.

Spam discipline
---------------
Resolver fires this exactly once per coupon transition (pending → won
or pending → lost). The sender swallows the result so a flaky push
service never bubbles up into resolver/recorder failures.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from sqlalchemy.orm import Session

from app.models.push import PushSubscription


logger = logging.getLogger(__name__)


def _vapid_claims() -> dict[str, str]:
    return {
        "sub": os.environ.get("VAPID_SUBJECT", "mailto:owner@cenkozgur.com"),
    }


def is_configured() -> bool:
    return bool(
        os.environ.get("VAPID_PRIVATE_KEY")
        and os.environ.get("VAPID_PUBLIC_KEY")
    )


def vapid_public_key() -> str | None:
    """Frontend reads this via /push/public-key to call subscribe."""
    return os.environ.get("VAPID_PUBLIC_KEY")


def send_to_subscription(
    db: Session,
    sub: PushSubscription,
    payload: dict[str, Any],
    ttl_seconds: int = 86400,
) -> bool:
    """Deliver one notification. Returns True on accepted (200/201/204).

    On 404/410 marks the subscription inactive — the browser unsubscribed
    or the endpoint rotated — so future broadcasts skip it.
    """
    if not is_configured():
        return False
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        logger.warning("pywebpush not installed — skipping push send")
        return False

    sub_info = {
        "endpoint": sub.endpoint,
        "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
    }
    try:
        webpush(
            subscription_info=sub_info,
            data=json.dumps(payload),
            vapid_private_key=os.environ["VAPID_PRIVATE_KEY"],
            vapid_claims=_vapid_claims(),
            ttl=ttl_seconds,
        )
        return True
    except WebPushException as exc:
        # pywebpush raises with response attached. 404/410 means dead.
        status = getattr(exc.response, "status_code", None)
        if status in (404, 410):
            sub.active = False
            db.add(sub)
            logger.info(
                "push endpoint dead, deactivated subscription id=%s status=%s",
                sub.id,
                status,
            )
        else:
            logger.warning(
                "push send failed for sub id=%s status=%s body=%s",
                sub.id,
                status,
                getattr(exc.response, "text", ""),
            )
        return False
    except Exception as exc:  # noqa: BLE001
        logger.exception("unexpected push error: %s", exc)
        return False


def broadcast(db: Session, payload: dict[str, Any]) -> int:
    """Send `payload` to every active subscription. Returns delivered count."""
    if not is_configured():
        return 0
    subs = db.query(PushSubscription).filter(PushSubscription.active.is_(True)).all()
    if not subs:
        return 0
    delivered = 0
    for s in subs:
        if send_to_subscription(db, s, payload):
            delivered += 1
    db.commit()
    return delivered
