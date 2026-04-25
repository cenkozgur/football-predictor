"""Sport-agnostic event row for non-football coverage.

Why a separate table from Match: Match carries football-specific machinery
(xG, ft/ht splits, live minute/score in football's 0-90 vocabulary, foreign
keys to Team rows that go through the football alias normaliser, odds
linkage, etc). Basketball / tennis / volleyball don't share that schema and
shoehorning them in would erode it.

This model is the read-only "what is on / when / where" view that the
Bugün Ne Var app needs. No prediction, no value-edge, no scoring system —
just enough to render an event card with kickoff time, broadcaster, and a
rough status.

External_ref convention:
    apisports:basketball:<api-football game id>
    apisports:tennis:<game id>
    apisports:volleyball:<game id>
Used both for upstream-id dedupe on re-ingest and as the stable key the
Bugün Ne Var app uses to track subscriptions.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class SportEvent(Base):
    __tablename__ = "sport_events"
    __table_args__ = (
        # Multiple ingest runs must not duplicate the same upstream row.
        UniqueConstraint("external_ref", name="uq_sport_event_external_ref"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    external_ref: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)

    # 'basketball' / 'tennis' / 'volleyball'
    sport: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    # League / competition code from upstream (e.g. 'NBA', 'EuroLeague',
    # 'BSL', 'ATP', 'WTA', 'CEV-CL', 'VNL').
    league: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    season: Mapped[str | None] = mapped_column(String(16), nullable=True)

    kickoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)

    home_team: Mapped[str] = mapped_column(String(96), nullable=False)
    away_team: Mapped[str | None] = mapped_column(String(96), nullable=True)
    # For tennis individual matches we still use home/away_team. For
    # tournaments-as-events (e.g. "Wimbledon — Day 4") away_team can be
    # null and home_team carries the round/session label.

    venue: Mapped[str | None] = mapped_column(String(128), nullable=True)
    broadcaster: Mapped[str | None] = mapped_column(String(128), nullable=True)

    status: Mapped[str] = mapped_column(String(16), default="scheduled", nullable=False)
