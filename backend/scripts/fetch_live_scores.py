"""Fetch in-play match scores from football-data.org and update our DB.

Writes `live_minute`, `live_home`, `live_away` on every currently in-play or
paused fixture we know about, and clears those columns (plus promotes the
score to ft_*) on matches that just finished. Meant to run on a short cron
(every 3-5 min) during match hours.

Why this script is separate from `football_data_org.py`
------------------------------------------------------
That module is the fixture-list ingester — it pulls SCHEDULED/TIMED rows
once a day. Live scores need a different query shape (no date range, just
status=IN_PLAY,PAUSED,FINISHED with a recent window) and a different
cadence (minutes, not hours). Keeping them separate means the daily ingest
stays fast and the live poller can fail without taking down fixture sync.

Usage
-----
    export FOOTBALL_DATA_ORG_KEY=your_key_here
    python -m scripts.fetch_live_scores
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import select

from app.db import SessionLocal
from app.models.match import Match
from app.models.team import Team


API_BASE = "https://api.football-data.org/v4"


def _client(api_key: str) -> httpx.Client:
    return httpx.Client(
        base_url=API_BASE,
        headers={"X-Auth-Token": api_key},
        timeout=30.0,
    )


def _parse_minute(minute_str: str | None) -> int | None:
    """Convert football-data.org minute strings to int.

    Examples: "45+2" → 47, "90+3" → 93, "45" → 45, None → None.
    We intentionally collapse stoppage time into the base minute's total so
    the UI can render a single number; separating HT/FT is easy from status.
    """
    if minute_str is None:
        return None
    m = re.match(r"(\d+)(?:\+(\d+))?", str(minute_str))
    if not m:
        return None
    base = int(m.group(1))
    extra = int(m.group(2)) if m.group(2) else 0
    return base + extra


def _find_match(db, api_row: dict[str, Any]) -> Match | None:
    """Locate the DB row for an api-returned fixture.

    We match by (home_team.name, away_team.name, kickoff date) rather than by
    external_id because the fixture ingester doesn't store the provider's id.
    Fuzzy-resolution of team names already happened at ingest time; here we
    trust the exact name we stored and fall back to a looser match only if
    needed.
    """
    home_name = (api_row.get("homeTeam") or {}).get("name")
    away_name = (api_row.get("awayTeam") or {}).get("name")
    utc_str = api_row.get("utcDate")
    if not (home_name and away_name and utc_str):
        return None

    try:
        kickoff = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
    except ValueError:
        return None

    # ±4h window around the stored kickoff — covers any clock jitter between
    # the fixture ingest snapshot and the live feed's reported UTC.
    lo = kickoff - timedelta(hours=4)
    hi = kickoff + timedelta(hours=4)

    stmt = (
        select(Match)
        .join(Team, Team.id == Match.home_team_id)
        .where(Match.kickoff >= lo, Match.kickoff <= hi)
    )
    candidates = list(db.scalars(stmt).all())
    for m in candidates:
        if m.home_team.name == home_name and m.away_team.name == away_name:
            return m
    # Last-ditch alias-based match — these names come from the provider so
    # lean on the same TEAM_ALIASES the fixture ingester uses.
    from app.ingestion.football_data_org import TEAM_ALIASES

    aliased_home = TEAM_ALIASES.get(home_name, home_name)
    aliased_away = TEAM_ALIASES.get(away_name, away_name)
    for m in candidates:
        if m.home_team.name == aliased_home and m.away_team.name == aliased_away:
            return m
    return None


def run() -> None:
    api_key = os.environ.get("FOOTBALL_DATA_ORG_KEY")
    if not api_key:
        print("FOOTBALL_DATA_ORG_KEY not set — skipping live score fetch.")
        sys.exit(0)

    updated_live = 0
    finished = 0
    not_found = 0

    with _client(api_key) as client, SessionLocal() as db:
        # Single global query — no per-league loop (we'd blow through the
        # 10 req/min quota fast). The response is capped at ~100 items/day
        # and matches only those currently in an in-play state.
        try:
            r = client.get("/matches", params={"status": "IN_PLAY,PAUSED,FINISHED"})
            r.raise_for_status()
        except httpx.HTTPError as exc:
            print(f"HTTP error fetching live scores: {exc}")
            sys.exit(1)

        data = r.json()
        rows = data.get("matches", [])
        print(f"Fetched {len(rows)} in-play/finished fixtures from provider")

        for api_row in rows:
            status = api_row.get("status", "")
            match = _find_match(db, api_row)
            if match is None:
                not_found += 1
                continue

            score = api_row.get("score") or {}
            full = score.get("fullTime") or {}
            home_goals = full.get("home")
            away_goals = full.get("away")

            if status in ("IN_PLAY", "PAUSED"):
                match.live_minute = _parse_minute(api_row.get("minute"))
                match.live_home = int(home_goals) if home_goals is not None else 0
                match.live_away = int(away_goals) if away_goals is not None else 0
                match.status = "in_play" if status == "IN_PLAY" else "paused"
                updated_live += 1
            elif status == "FINISHED":
                # Mirror the fixture ingester's settlement: copy score into ft_*,
                # flip status, clear live_*. Only write if ft_* is still null so
                # we don't stomp on a later manual correction.
                if match.ft_home is None and home_goals is not None:
                    match.ft_home = int(home_goals)
                if match.ft_away is None and away_goals is not None:
                    match.ft_away = int(away_goals)
                match.live_minute = None
                match.live_home = None
                match.live_away = None
                match.status = "finished"
                finished += 1

        db.commit()
        print(
            f"Live updates: {updated_live} in-play, {finished} newly finished, "
            f"{not_found} unmatched (likely leagues we don't ingest)."
        )


def main() -> None:
    run()


if __name__ == "__main__":
    main()
