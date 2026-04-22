"""Per-match team availability: injuries, suspensions, missing starters.

Why this table exists
---------------------
Dixon-Coles treats a team as a single coefficient — it has no concept of
"star striker out injured" or "first-choice keeper suspended." An 11-player
change of one key name can swing a goal expectation by 0.2-0.4 goals, which
is huge at coupon-composition scale. This table stores that pre-match signal
so the composer can nudge its picks away from teams missing decisive players.

Shape
-----
One row per (match, team) pair. `key_absences` is a small JSON list of
    {name, position, reason} so the UI can display "Maç öncesi eksikler:
    Haaland (sakat), Rodri (ceza)" without another query.

Why not a normalized Player table?
---------------------------------
We don't ingest rosters — api-football returns the name + position string
directly in the injuries/lineups endpoints, and the composer only needs to
know "how many key players are out," not "which specific player." Keeping
this denormalized means one less join and one less sync pipeline.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class TeamAvailability(Base):
    __tablename__ = "team_availability"
    __table_args__ = (
        UniqueConstraint("match_id", "team_id", name="uq_availability_match_team"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), nullable=False, index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False, index=True)

    # Count of notable absences (injuries + suspensions combined). A crude
    # signal but cheap to compute and surprisingly predictive; the composer
    # scales impact by this rather than by player ratings we don't have.
    absent_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # "Key" = starter-likely or historically high-minute. api-football's
    # /injuries endpoint already filters to probable XI — we treat every
    # returned row as key, since non-starters aren't usually listed there.
    key_absent_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # [{"name": str, "position": str, "reason": "injury"|"suspension"}]
    # Stored as JSON because we never filter by it, only read it out for UI.
    key_absences: Mapped[list[dict] | None] = mapped_column(JSON, nullable=True)

    # Freshness: rows older than ~24h are stale (lineup news moves fast).
    # The fetcher refreshes daily; composer can check this before trusting it.
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
