"""Push subscription endpoints.

GET  /push/public-key   — VAPID public key the browser needs for subscribe
POST /push/subscribe    — register a new browser endpoint
DEL  /push/{id}         — explicit unsubscribe
POST /push/test         — send the calling subscription a hello payload
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.push import PushSubscription
from app.services.push import (
    broadcast,
    is_configured,
    send_to_subscription,
    vapid_public_key,
)


router = APIRouter()


class SubscribeIn(BaseModel):
    endpoint: str
    p256dh: str
    auth: str
    label: str | None = None


@router.get("/public-key")
def get_public_key() -> dict[str, Any]:
    """Frontend pulls this once before calling subscribe(). Empty when
    server isn't configured for push — UI hides the bell in that case."""
    return {
        "configured": is_configured(),
        "public_key": vapid_public_key(),
    }


@router.post("/subscribe")
def subscribe(body: SubscribeIn, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Idempotent. Same endpoint twice = update label + last_seen, not
    duplicate row. Browsers can call this on every app load to refresh
    the row's freshness without needing to track 'have I subscribed'."""
    existing = db.scalar(
        select(PushSubscription).where(PushSubscription.endpoint == body.endpoint)
    )
    if existing:
        existing.p256dh = body.p256dh
        existing.auth = body.auth
        if body.label:
            existing.label = body.label
        existing.active = True
        existing.last_seen_at = datetime.now(tz=timezone.utc)
        db.commit()
        return {"id": existing.id, "created": False}

    sub = PushSubscription(
        endpoint=body.endpoint,
        p256dh=body.p256dh,
        auth=body.auth,
        label=body.label,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return {"id": sub.id, "created": True}


@router.delete("/{sub_id}")
def unsubscribe(sub_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    sub = db.get(PushSubscription, sub_id)
    if sub is None:
        raise HTTPException(404, "subscription not found")
    sub.active = False
    db.commit()
    return {"ok": True, "id": sub.id}


@router.get("/subscriptions/count")
def subscription_count(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Debug-only: how many subscriptions exist, active and dead. Helpful
    when 'Test gönder' returns delivered=0 — distinguishes 'subscribe POST
    never landed' from 'subscribe landed but endpoint already inactive'."""
    rows = db.query(PushSubscription).all()
    active = sum(1 for r in rows if r.active)
    return {
        "total": len(rows),
        "active": active,
        "inactive": len(rows) - active,
        "endpoints": [
            {
                "id": r.id,
                "active": r.active,
                "endpoint_host": r.endpoint.split("//", 1)[-1].split("/", 1)[0] if r.endpoint else None,
                "label": r.label,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


@router.post("/test")
def send_test(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Broadcast a hello to every active subscription. Useful from the
    'Bildirim test et' button to confirm the wiring before live events."""
    if not is_configured():
        raise HTTPException(503, "push not configured (VAPID keys missing)")
    delivered = broadcast(
        db,
        {
            "title": "Banko Kupon",
            "body": "Bildirimler aktif — kupon sonuçları bu kanaldan gelecek.",
            "tag": "test",
            "url": "/istatistikler",
        },
    )
    return {"delivered": delivered}
