from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.team import Team


class Match(Base):
    __tablename__ = "matches"
    __table_args__ = (
        UniqueConstraint("home_team_id", "away_team_id", "kickoff", name="uq_match_teams_time"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    league: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    season: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    kickoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)

    home_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False)
    away_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False)

    # Final / half-time scores, nullable until the match is played
    ft_home: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ft_away: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ht_home: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ht_away: Mapped[int | None] = mapped_column(Integer, nullable=True)

    status: Mapped[str] = mapped_column(String(16), default="scheduled", nullable=False)

    # Expected goals (Understat). Nullable: only populated for top-5 leagues
    # that Understat covers (EPL, BL1, SA, PD, FL1). Other leagues fall back
    # to ft_home/ft_away in the model.
    xg_home: Mapped[float | None] = mapped_column(Float, nullable=True)
    xg_away: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Live (in-play) tracking — populated only while status in
    # ('in_play', 'paused') and cleared when the match finishes. `live_minute`
    # is the match clock (0-120+), not wall-clock time.
    live_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    live_home: Mapped[int | None] = mapped_column(Integer, nullable=True)
    live_away: Mapped[int | None] = mapped_column(Integer, nullable=True)

    home_team: Mapped[Team] = relationship(foreign_keys=[home_team_id])
    away_team: Mapped[Team] = relationship(foreign_keys=[away_team_id])
