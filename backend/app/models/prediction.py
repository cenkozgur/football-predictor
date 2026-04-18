from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Prediction(Base):
    """One row per (match, model_version). The full multi-market blob lives in `payload`.

    payload example:
    {
        "1X2": {"1": 0.52, "X": 0.26, "2": 0.22},
        "over_under": {"2.5": 0.58, "3.5": 0.30},
        "btts": {"yes": 0.61, "no": 0.39},
        "correct_score_top5": [{"2-1": 0.11}, ...],
        ...
    }
    """

    __tablename__ = "predictions"

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), nullable=False, index=True)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    lambda_home: Mapped[float] = mapped_column(Float, nullable=False)
    lambda_away: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
