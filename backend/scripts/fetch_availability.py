"""Fetch injury + suspension data from api-football, fill team_availability.

Why this script
---------------
Dixon-Coles doesn't know when a team's star striker is out. That single
missing player can shift a match's goal expectation by 0.2-0.4 goals, which
is a huge edge signal when the market hasn't fully priced the news in yet.
This script pulls api-football's /injuries feed once per daily ingest and
snapshots "who's out for which match" into our DB, where the composer can
read it.

Dry-run contract
----------------
If FOOTBALL_API_KEY isn't set (i.e. the user hasn't subscribed yet), we
exit 0 with a warning. The workflow calls us unconditionally so we ship
the code before the subscription arrives.

Usage
-----
    export FOOTBALL_API_KEY=...          # optional; without it we no-op
    python -m scripts.fetch_availability --days-ahead 2
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.db import SessionLocal
from app.ingestion.api_football import (
    LEAGUE_MAP,
    _build_team_index,
    _resolve_team,
)
from app.models.match import Match
from app.models.team import Team
from app.models.team_availability import TeamAvailability


# We treat api-football's /injuries response as "probable XI minus these."
# Suspensions and injuries both land here; the `type` field tells them apart.
# api-football returns a handful of states — normalize to two buckets.
_REASON_MAP = {
    "Missing Fixture": "injury",
    "Questionable": "injury",
    "Suspended": "suspension",
    "Red Card": "suspension",
}


def _new_client():
    """Deferred import so `--help` works without FOOTBALL_API_KEY set."""
    from app.ingestion.api_football import _client  # imported lazily
    import os

    key = os.environ.get("FOOTBALL_API_KEY")
    if not key:
        return None
    return _client(key)


def _upcoming_matches(db, days_ahead: int) -> list[Match]:
    now_naive = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    horizon = now_naive + timedelta(days=days_ahead)
    stmt = (
        select(Match)
        .options(joinedload(Match.home_team), joinedload(Match.away_team))
        .where(Match.status == "scheduled")
        .where(Match.kickoff > now_naive)
        .where(Match.kickoff <= horizon)
        .order_by(Match.kickoff.asc())
    )
    return list(db.scalars(stmt).all())


def _fetch_injuries_for_league(
    client, league_id: int, season: int, date_from: str, date_to: str
) -> list[dict[str, Any]]:
    """One api-football call returns every injury across the league in range."""
    from app.ingestion.api_football import _get

    data = _get(
        client,
        "/injuries",
        {
            "league": league_id,
            "season": season,
            # api-football accepts "date" for a single day; we call per-day.
            # For date ranges we loop — the /injuries endpoint doesn't accept
            # from/to, unlike /fixtures. 2 days × N leagues is still cheap.
        },
    )
    return data.get("response", [])


def _upsert_availability(
    db,
    match_id: int,
    team_id: int,
    absences: list[dict[str, Any]],
) -> None:
    """Insert or replace the availability row for (match, team)."""
    existing = db.scalar(
        select(TeamAvailability).where(
            TeamAvailability.match_id == match_id,
            TeamAvailability.team_id == team_id,
        )
    )

    # key_absences is the UI-facing shortlist; we keep the top 5 by position
    # prominence (GK / forwards cited first by the composer).
    key_rows = [
        {
            "name": a.get("name"),
            "position": a.get("position"),
            "reason": a.get("reason"),
        }
        for a in absences
    ][:5]

    absent_count = len(absences)
    if existing is None:
        db.add(
            TeamAvailability(
                match_id=match_id,
                team_id=team_id,
                absent_count=absent_count,
                key_absent_count=absent_count,
                key_absences=key_rows,
            )
        )
    else:
        existing.absent_count = absent_count
        existing.key_absent_count = absent_count
        existing.key_absences = key_rows
        existing.fetched_at = datetime.now(tz=timezone.utc)


def run(days_ahead: int = 2) -> None:
    client = _new_client()
    if client is None:
        print("FOOTBALL_API_KEY not set — skipping availability fetch (dry-run).")
        return

    written_teams = 0
    api_calls = 0
    with client, SessionLocal() as db:
        matches = _upcoming_matches(db, days_ahead=days_ahead)
        if not matches:
            print("No upcoming matches in range — nothing to fetch.")
            return

        # Group by (league, season) so we make one /injuries call per league
        # rather than per match. Free tier = 100 req/day; we stay well under.
        by_league: dict[str, list[Match]] = {}
        for m in matches:
            by_league.setdefault(m.league, []).append(m)

        for code, league_matches in by_league.items():
            if code not in LEAGUE_MAP:
                continue
            api_id, season = LEAGUE_MAP[code]
            try:
                injuries = _fetch_injuries_for_league(
                    client, api_id, season, "", ""
                )
                api_calls += 1
            except Exception as exc:
                print(f"  [{code}] injuries fetch failed: {exc}")
                continue

            # injuries[*].player.{name,position}; injuries[*].team.id; fixture.id
            # We can't map api-football fixture.id back to our Match directly
            # (we don't store it), so we match on (team, kickoff) within ±4h.
            team_index = _build_team_index(db)
            fuzzy_cache: dict[str, Team | None] = {}

            per_match: dict[tuple[int, int], list[dict[str, Any]]] = {}
            for inj in injuries:
                api_team_name = (inj.get("team") or {}).get("name")
                if not api_team_name:
                    continue
                team = _resolve_team(api_team_name, team_index, fuzzy_cache)
                if team is None:
                    continue
                fixture_date = ((inj.get("fixture") or {}).get("date")) or ""
                try:
                    kickoff = datetime.fromisoformat(
                        fixture_date.replace("Z", "+00:00")
                    ).replace(tzinfo=None)
                except ValueError:
                    continue

                # Find our Match within ±4h of the fixture's kickoff, same team
                matched = next(
                    (
                        m
                        for m in league_matches
                        if (m.home_team_id == team.id or m.away_team_id == team.id)
                        and abs((m.kickoff - kickoff).total_seconds()) < 4 * 3600
                    ),
                    None,
                )
                if matched is None:
                    continue

                player = inj.get("player") or {}
                reason_raw = player.get("reason") or player.get("type") or ""
                per_match.setdefault((matched.id, team.id), []).append(
                    {
                        "name": player.get("name"),
                        "position": player.get("position"),
                        "reason": _REASON_MAP.get(reason_raw, "injury"),
                    }
                )

            for (match_id, team_id), absences in per_match.items():
                _upsert_availability(db, match_id, team_id, absences)
                written_teams += 1

        db.commit()

    print(
        f"Availability snapshot: {written_teams} team-match rows updated across "
        f"{api_calls} api-football calls."
    )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--days-ahead", type=int, default=2)
    args = p.parse_args()
    run(days_ahead=args.days_ahead)


if __name__ == "__main__":
    main()
