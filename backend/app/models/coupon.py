"""Historical coupon records with per-leg resolution.

Why this table exists
---------------------
`/coupons` recomputes suggestions on demand and doesn't persist anything, so
there's no way to look back and ask "which coupons did we suggest last week,
and how many hit?". To differentiate ourselves from bilyoner we need to show
that our strategy *has a track record*, not just today's picks. This table is
that record: every suggested coupon is snapshotted here on the day it was
generated, then resolved once the underlying matches finish.

Shape
-----
    Coupon:    one row per suggested coupon (date, leg count, combined odds, hit)
    CouponLeg: one row per pick inside a coupon (market, selection, prob, result)

We intentionally keep legs relational instead of stuffing them into JSON so the
UI can render per-leg ✓/✗ cheaply, and so we can aggregate hit rates by market
across the whole history without decoding blobs.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Coupon(Base):
    __tablename__ = "coupons"
    __table_args__ = (
        # Same day + same combined odds + same signature = same coupon. Stops
        # the daily recorder from inserting duplicates when it runs twice.
        UniqueConstraint("generated_on", "signature", name="uq_coupon_day_signature"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # Calendar date (UTC) the coupon was generated, independent of kickoff time.
    # Used to query "coupons from last week" without worrying about timezones.
    generated_on: Mapped[str] = mapped_column(String(10), index=True, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Stable hash of (sorted match_id|market|selection tuples) so the same
    # coupon recorded twice in one day is collapsed.
    signature: Mapped[str] = mapped_column(String(64), nullable=False)

    # "primary" (highest-confidence of the day) vs "alternative" (diversity picks).
    kind: Mapped[str] = mapped_column(String(16), default="primary", nullable=False)

    num_legs: Mapped[int] = mapped_column(Integer, nullable=False)
    combined_prob: Mapped[float] = mapped_column(Float, nullable=False)
    combined_odds: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Engine's composite (prob+value+motivation+form) averaged across legs —
    # our own pre-kickoff confidence signal, separate from market-derived prob.
    avg_composite: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Resolution: null while any leg is pending, true/false once all legs settle.
    hit: Mapped[bool | None] = mapped_column(Boolean, nullable=True, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    legs: Mapped[list["CouponLeg"]] = relationship(
        back_populates="coupon",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class CouponLeg(Base):
    __tablename__ = "coupon_legs"

    id: Mapped[int] = mapped_column(primary_key=True)
    coupon_id: Mapped[int] = mapped_column(
        ForeignKey("coupons.id", ondelete="CASCADE"), nullable=False, index=True
    )
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), nullable=False, index=True)

    market: Mapped[str] = mapped_column(String(32), nullable=False)
    selection: Mapped[str] = mapped_column(String(16), nullable=False)
    # User-facing Turkish label as shown in the app ("2.5 Üst", "KG Var", "1X").
    selection_label: Mapped[str] = mapped_column(String(64), nullable=False)

    prob: Mapped[float] = mapped_column(Float, nullable=False)
    book_odds: Mapped[float | None] = mapped_column(Float, nullable=True)
    value_edge: Mapped[float | None] = mapped_column(Float, nullable=True)
    composite: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Per-leg resolution. Null while the match is unplayed.
    hit: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    coupon: Mapped[Coupon] = relationship(back_populates="legs")
