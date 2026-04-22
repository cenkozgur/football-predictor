from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.match import Match

router = APIRouter()


class MatchOut(BaseModel):
    id: int
    league: str
    season: str
    kickoff: datetime
    home_team: str
    away_team: str
    status: str
    ft_home: int | None = None
    ft_away: int | None = None
    # Live (in-play) snapshot; null unless status in ('in_play','paused').
    live_minute: int | None = None
    live_home: int | None = None
    live_away: int | None = None


_LIVE_STATUSES = ("in_play", "paused")


def _to_out(m: Match) -> "MatchOut":
    return MatchOut(
        id=m.id,
        league=m.league,
        season=m.season,
        kickoff=m.kickoff,
        home_team=m.home_team.name,
        away_team=m.away_team.name,
        status=m.status,
        ft_home=m.ft_home,
        ft_away=m.ft_away,
        live_minute=m.live_minute,
        live_home=m.live_home,
        live_away=m.live_away,
    )


@router.get("", response_model=list[MatchOut])
def list_matches(
    league: str | None = Query(default=None),
    upcoming: bool = Query(default=True),
    status: str | None = Query(
        default=None,
        description="Filter by match status (scheduled, finished, live). Overrides `upcoming`.",
    ),
    limit: int = Query(default=50, le=200),
    db: Session = Depends(get_db),
) -> list[MatchOut]:
    stmt = select(Match)
    if league:
        stmt = stmt.where(Match.league == league)
    # `status` takes priority over the older `upcoming` boolean because the UI
    # needs an explicit "Biten" mode; without it the server silently returns
    # upcoming rows and the "Biten" filter shows yaklaşan cards. `live` is a
    # virtual status covering both IN_PLAY and PAUSED.
    if status == "live":
        stmt = stmt.where(Match.status.in_(_LIVE_STATUSES))
        stmt = stmt.order_by(Match.kickoff.asc())
    elif status:
        stmt = stmt.where(Match.status == status)
        if status == "finished":
            stmt = stmt.order_by(Match.kickoff.desc())
        else:
            stmt = stmt.order_by(Match.kickoff.asc())
    elif upcoming:
        stmt = stmt.where(Match.kickoff >= datetime.now(tz=timezone.utc))
        stmt = stmt.order_by(Match.kickoff.asc())
    else:
        stmt = stmt.order_by(Match.kickoff.asc())
    stmt = stmt.limit(limit)

    rows = db.scalars(stmt).all()
    return [_to_out(m) for m in rows]


@router.get("/live", response_model=list[MatchOut])
def live_matches(db: Session = Depends(get_db)) -> list[MatchOut]:
    """Currently in-play or paused matches, with live score + minute."""
    stmt = (
        select(Match)
        .where(Match.status.in_(_LIVE_STATUSES))
        .order_by(Match.kickoff.asc())
    )
    return [_to_out(m) for m in db.scalars(stmt).all()]


@router.get("/{match_id}", response_model=MatchOut)
def get_match(match_id: int, db: Session = Depends(get_db)) -> MatchOut:
    m = db.get(Match, match_id)
    if m is None:
        raise HTTPException(status_code=404, detail="Match not found")
    return _to_out(m)
