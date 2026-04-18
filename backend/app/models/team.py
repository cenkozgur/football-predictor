from sqlalchemy import Float, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Team(Base):
    __tablename__ = "teams"
    __table_args__ = (UniqueConstraint("name", "country", name="uq_team_name_country"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    country: Mapped[str | None] = mapped_column(String(64), nullable=True)
    league: Mapped[str | None] = mapped_column(String(64), nullable=True)
    club_elo: Mapped[float | None] = mapped_column(Float, nullable=True)
