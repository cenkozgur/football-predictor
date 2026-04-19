"""Derive a league standings snapshot from finished matches.

We don't store standings in the DB — instead we compute them on demand from
the matches table for a given (league, season, as-of date). This keeps the
schema simple and is always in sync with ingested results.

The table returned here is the input to `motivation.py`, which turns
"team X is 3 points off relegation with 5 games left" into a scalar signal
the model can reason about.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.match import Match


@dataclass
class TeamRow:
    team_id: int
    team_name: str
    played: int
    wins: int
    draws: int
    losses: int
    goals_for: int
    goals_against: int

    @property
    def points(self) -> int:
        return 3 * self.wins + self.draws

    @property
    def goal_diff(self) -> int:
        return self.goals_for - self.goals_against


@dataclass
class Standings:
    league: str
    season: str
    asof: datetime
    rows: list[TeamRow]           # ordered by points desc, gd desc, gf desc
    total_teams: int
    matches_played_total: int     # sum across all teams / 2
    matches_scheduled_total: int  # finished + future in this (league, season)

    def rank_of(self, team_id: int) -> int | None:
        for i, r in enumerate(self.rows):
            if r.team_id == team_id:
                return i + 1
        return None

    def row_of(self, team_id: int) -> TeamRow | None:
        for r in self.rows:
            if r.team_id == team_id:
                return r
        return None

    @property
    def matches_remaining_total(self) -> int:
        return self.matches_scheduled_total - self.matches_played_total


def build_standings(
    db: Session,
    league: str,
    season: str,
    asof: datetime,
) -> Standings:
    """Build the table for `league`/`season`, using only finished matches
    that kicked off strictly before `asof`.

    `asof` is exclusive so that when we predict match M we never peek at M's
    own result even if it is already recorded (e.g. during backtesting).
    """
    # All matches in this league+season — we count scheduled ones for the
    # "remaining fixtures" signal, and finished-before-asof ones for the table.
    all_matches = db.scalars(
        select(Match)
        .where(Match.league == league, Match.season == season)
        .options(selectinload(Match.home_team), selectinload(Match.away_team))
    ).all()

    if not all_matches:
        return Standings(
            league=league,
            season=season,
            asof=asof,
            rows=[],
            total_teams=0,
            matches_played_total=0,
            matches_scheduled_total=0,
        )

    # Aggregate per-team stats from finished matches before asof.
    stats: dict[int, TeamRow] = {}

    def _touch(team_id: int, team_name: str) -> TeamRow:
        r = stats.get(team_id)
        if r is None:
            r = TeamRow(
                team_id=team_id,
                team_name=team_name,
                played=0, wins=0, draws=0, losses=0,
                goals_for=0, goals_against=0,
            )
            stats[team_id] = r
        return r

    matches_played = 0
    for m in all_matches:
        # Register every team that appears in this league-season, even if it
        # hasn't played yet — so a freshly-promoted side shows up in the table.
        _touch(m.home_team_id, m.home_team.name)
        _touch(m.away_team_id, m.away_team.name)

        if m.status != "finished" or m.ft_home is None or m.ft_away is None:
            continue
        if _as_naive_utc(m.kickoff) >= _as_naive_utc(asof):
            continue

        h = _touch(m.home_team_id, m.home_team.name)
        a = _touch(m.away_team_id, m.away_team.name)
        h.played += 1
        a.played += 1
        h.goals_for += m.ft_home
        h.goals_against += m.ft_away
        a.goals_for += m.ft_away
        a.goals_against += m.ft_home
        if m.ft_home > m.ft_away:
            h.wins += 1
            a.losses += 1
        elif m.ft_home < m.ft_away:
            a.wins += 1
            h.losses += 1
        else:
            h.draws += 1
            a.draws += 1
        matches_played += 1

    rows = sorted(
        stats.values(),
        key=lambda r: (r.points, r.goal_diff, r.goals_for),
        reverse=True,
    )

    return Standings(
        league=league,
        season=season,
        asof=asof,
        rows=rows,
        total_teams=len(stats),
        matches_played_total=matches_played,
        matches_scheduled_total=len(all_matches),
    )


def _as_naive_utc(dt: datetime) -> datetime:
    # SQLite stores tz-aware datetimes as naive UTC; normalize both sides
    # so comparisons never raise.
    if dt.tzinfo is None:
        return dt
    return dt.replace(tzinfo=None)
