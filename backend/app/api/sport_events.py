"""Read-only API for non-football sport events.

Single endpoint covering basketball / tennis / volleyball etc — they
share `SportEvent` rather than each getting its own table. The Bugün Ne
Var web app calls this; nothing in the predictor pipeline does.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.sport_event import SportEvent

router = APIRouter()


class SportEventOut(BaseModel):
    id: int
    external_ref: str
    sport: str
    league: str
    season: str | None = None
    kickoff: datetime
    home_team: str
    away_team: str | None = None
    venue: str | None = None
    broadcaster: str | None = None
    status: str


def _to_out(e: SportEvent) -> SportEventOut:
    return SportEventOut(
        id=e.id,
        external_ref=e.external_ref,
        sport=e.sport,
        league=e.league,
        season=e.season,
        kickoff=e.kickoff,
        home_team=e.home_team,
        away_team=e.away_team,
        venue=e.venue,
        broadcaster=e.broadcaster,
        status=e.status,
    )


@router.get("", response_model=list[SportEventOut])
def list_sport_events(
    sport: str | None = Query(default=None, description="basketball / tennis / volleyball"),
    league: str | None = Query(default=None, description="NBA / EuroLeague / BSL etc."),
    upcoming: bool = Query(default=True),
    limit: int = Query(default=200, le=500),
    db: Session = Depends(get_db),
) -> list[SportEventOut]:
    stmt = select(SportEvent)
    if sport:
        stmt = stmt.where(SportEvent.sport == sport)
    if league:
        stmt = stmt.where(SportEvent.league == league)
    if upcoming:
        stmt = stmt.where(SportEvent.kickoff >= datetime.now(tz=timezone.utc))
    stmt = stmt.order_by(SportEvent.kickoff.asc()).limit(limit)
    return [_to_out(e) for e in db.scalars(stmt).all()]
