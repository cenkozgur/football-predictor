"""Per-device Web Push subscription records.

Why this table
--------------
The Banko Kupon UI is a web app, not a native mobile one — there's no
APNS/FCM device registry. The Web Push API hands the browser a unique
endpoint URL plus two encryption keys (`p256dh` + `auth`); the server
later POSTs to that endpoint to deliver a notification. We persist the
triple here so resolver hooks (and any future broadcast features) can
look up subscribers and fan out.

No user accounts in the app yet — each subscription is identified only
by its endpoint URL (which is itself unique per browser install). When
auth lands later we can attach a user_id; until then the endpoint IS
the identity.

Cleanup
-------
Browsers rotate or expire push endpoints; pywebpush returns 404/410 in
that case. The sender code marks rows as inactive on those errors so
we stop targeting dead endpoints, but keeps the row for audit. A
periodic prune can drop entries older than 90 days that have stayed
inactive — not implemented here, just a future hook.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"
    __table_args__ = (
        UniqueConstraint("endpoint", name="uq_push_subscription_endpoint"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # The push service endpoint URL. Looks like:
    # https://fcm.googleapis.com/fcm/send/<token>
    # https://web.push.apple.com/<token>
    endpoint: Mapped[str] = mapped_column(String(512), nullable=False)

    # Encryption keys provided by the browser when the user grants
    # permission. We never look at them ourselves — pywebpush uses them
    # to encrypt the payload bound for this specific device.
    p256dh: Mapped[str] = mapped_column(String(255), nullable=False)
    auth: Mapped[str] = mapped_column(String(255), nullable=False)

    # Optional human label so a user with multiple devices can tell them
    # apart later (e.g. "iPhone Safari", "Mac Chrome"). Browser doesn't
    # send this; the frontend can fill it from navigator.userAgent.
    label: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # Flipped to False when the push service rejects the endpoint
    # (404/410). We keep the row for audit but skip it on broadcasts.
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
