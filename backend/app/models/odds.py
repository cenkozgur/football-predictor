from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Odds(Base):
    """Time-series of odds for a match, keyed by source + market + selection.

    Examples of (market, selection):
        ("1X2", "1"), ("1X2", "X"), ("1X2", "2")
        ("OU_2.5", "over"), ("OU_2.5", "under")
        ("BTTS", "yes"), ("BTTS", "no")
        ("CS", "2-1"), ("CS", "1-1"), ...
    """

    __tablename__ = "odds"
    __table_args__ = (
        Index("ix_odds_match_market", "match_id", "market", "selection"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)  # e.g. "bilyoner", "pinnacle"
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    selection: Mapped[str] = mapped_column(String(32), nullable=False)
    decimal_odds: Mapped[float] = mapped_column(Float, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
