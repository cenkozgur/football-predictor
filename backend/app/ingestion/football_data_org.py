"""football-data.org ingester for upcoming fixtures.

Why this exists
---------------
api-football.com's free plan blocks current seasons. football-data.org's free
tier exposes upcoming fixtures for the big-5 + Netherlands + Portugal + ELC
(plus UCL / UEL), which covers most of what we generate coupons for.

No odds from this source — we rely on the Dixon-Coles model's own probabilities
to pick coupon legs.

Usage
-----
    export FOOTBALL_DATA_ORG_KEY=your_key_here
    python -m app.ingestion.football_data_org --days-ahead 7
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from difflib import get_close_matches
from typing import Any

import httpx
from sqlalchemy import select

from app.db import SessionLocal
from app.models.match import Match
from app.models.team import Team


API_BASE = "https://api.football-data.org/v4"


# Internal code → football-data.org competition code.
# Free tier supports: PL, BL1, SA, PD, FL1, DED, PPL, ELC, CL, EL.
LEAGUE_MAP: dict[str, str] = {
    "E0":  "PL",   # Premier League
    "D1":  "BL1",  # Bundesliga
    "I1":  "SA",   # Serie A
    "SP1": "PD",   # La Liga (Primera División)
    "F1":  "FL1",  # Ligue 1
    "N1":  "DED",  # Eredivisie
    "P1":  "PPL",  # Primeira Liga
    "E1":  "ELC",  # Championship
}


# api response → Team.name in our DB
TEAM_ALIASES: dict[str, str] = {
    "Manchester United FC": "Man United",
    "Manchester City FC": "Man City",
    "Tottenham Hotspur FC": "Tottenham",
    "Nottingham Forest FC": "Nott'm Forest",
    "Newcastle United FC": "Newcastle",
    "Wolverhampton Wanderers FC": "Wolves",
    "Sheffield United FC": "Sheffield United",
    "Paris Saint-Germain FC": "Paris SG",
    "FC Bayern München": "Bayern Munich",
    "Borussia Mönchengladbach": "M'gladbach",
    "Brighton & Hove Albion FC": "Brighton",
    "West Ham United FC": "West Ham",
    "AFC Bournemouth": "Bournemouth",
    "Leicester City FC": "Leicester",
    "Ipswich Town FC": "Ipswich",
    "Aston Villa FC": "Aston Villa",
    "Everton FC": "Everton",
    "Fulham FC": "Fulham",
    "Liverpool FC": "Liverpool",
    "Chelsea FC": "Chelsea",
    "Arsenal FC": "Arsenal",
    "Crystal Palace FC": "Crystal Palace",
}


def _client(api_key: str) -> httpx.Client:
    return httpx.Client(
        base_url=API_BASE,
        headers={"X-Auth-Token": api_key},
        timeout=30.0,
    )


def _get(client: httpx.Client, path: str, params: dict[str, Any]) -> dict[str, Any]:
    r = client.get(path, params=params)
    if r.status_code == 429:
        # free tier: 10 req/min — back off and retry once
        time.sleep(8)
        r = client.get(path, params=params)
    r.raise_for_status()
    return r.json()


def _normalize(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"\b(fc|cf|afc|sc|ac|as|ss|club|de|el|la|le|the)\b", " ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _build_team_index(db) -> dict[str, Team]:
    teams = list(db.scalars(select(Team)).all())
    return {_normalize(t.name): t for t in teams}


def _resolve_team(
    name: str,
    team_index: dict[str, Team],
    fuzzy_cache: dict[str, Team | None],
) -> Team | None:
    if name in TEAM_ALIASES:
        norm = _normalize(TEAM_ALIASES[name])
        if norm in team_index:
            return team_index[norm]

    norm = _normalize(name)
    if norm in team_index:
        return team_index[norm]

    if name in fuzzy_cache:
        return fuzzy_cache[name]
    keys = list(team_index.keys())
    close = get_close_matches(norm, keys, n=1, cutoff=0.86)
    team = team_index[close[0]] if close else None
    fuzzy_cache[name] = team
    return team


def fetch_fixtures(
    api_key: str,
    leagues: list[str],
    days_ahead: int,
) -> list[dict[str, Any]]:
    date_from = datetime.now(tz=timezone.utc).date().isoformat()
    date_to = (datetime.now(tz=timezone.utc).date() + timedelta(days=days_ahead)).isoformat()
    fixtures: list[dict[str, Any]] = []
    with _client(api_key) as client:
        for code in leagues:
            if code not in LEAGUE_MAP:
                print(f"  [{code}] not in LEAGUE_MAP — skipping")
                continue
            api_code = LEAGUE_MAP[code]
            try:
                data = _get(client, f"/competitions/{api_code}/matches", {
                    "dateFrom": date_from, "dateTo": date_to,
                    "status": "SCHEDULED,TIMED",
                })
            except httpx.HTTPStatusError as exc:
                print(f"  [{code}] HTTP {exc.response.status_code}: {exc.response.text[:200]}")
                continue
            rows = data.get("matches", [])
            print(f"  [{code}={api_code}] fixtures {date_from}..{date_to}: {len(rows)}")
            for row in rows:
                row["_our_league_code"] = code
                row["_our_season"] = str(
                    row.get("season", {}).get("startDate", "")[:4] or ""
                )
                fixtures.append(row)
            time.sleep(6.5)  # free tier: 10 req/min
    return fixtures


def upsert_fixtures(db, fixtures: list[dict[str, Any]]) -> tuple[int, int, list[str]]:
    team_index = _build_team_index(db)
    fuzzy_cache: dict[str, Team | None] = {}
    written = 0
    skipped = 0
    unmatched: set[str] = set()

    for fx in fixtures:
        home_name = fx["homeTeam"]["name"]
        away_name = fx["awayTeam"]["name"]
        home = _resolve_team(home_name, team_index, fuzzy_cache)
        away = _resolve_team(away_name, team_index, fuzzy_cache)
        if home is None:
            unmatched.add(home_name)
        if away is None:
            unmatched.add(away_name)
        if home is None or away is None:
            skipped += 1
            continue

        kickoff_iso = fx["utcDate"]
        kickoff = datetime.fromisoformat(kickoff_iso.replace("Z", "+00:00"))
        kickoff_naive = kickoff.astimezone(timezone.utc).replace(tzinfo=None)

        existing = db.execute(
            select(Match).where(
                Match.home_team_id == home.id,
                Match.away_team_id == away.id,
                Match.kickoff == kickoff_naive,
            )
        ).scalar_one_or_none()

        if existing is None:
            db.add(Match(
                league=fx["_our_league_code"],
                season=fx["_our_season"],
                kickoff=kickoff_naive,
                home_team_id=home.id,
                away_team_id=away.id,
                status="scheduled",
            ))
            written += 1

    db.commit()
    return written, skipped, sorted(unmatched)


def run(leagues: list[str], days_ahead: int) -> None:
    api_key = os.environ.get("FOOTBALL_DATA_ORG_KEY")
    if not api_key:
        print("Set FOOTBALL_DATA_ORG_KEY environment variable.")
        sys.exit(1)

    print(f"Fetching fixtures for {len(leagues)} leagues over next {days_ahead} days…")
    fixtures = fetch_fixtures(api_key, leagues, days_ahead)
    print(f"Total fixtures retrieved: {len(fixtures)}")

    with SessionLocal() as db:
        written, skipped, unmatched = upsert_fixtures(db, fixtures)
        print(f"Inserted {written} new fixtures, skipped {skipped} (unmatched teams).")
        if unmatched:
            print("\nUnmatched team names (add to TEAM_ALIASES):")
            for n in unmatched:
                print(f"  {n!r}")


def main() -> None:
    p = argparse.ArgumentParser(description="football-data.org ingester")
    p.add_argument("--leagues", default=None,
                   help="Comma-separated internal codes. Default: all mapped.")
    p.add_argument("--days-ahead", type=int, default=7)
    args = p.parse_args()
    leagues = args.leagues.split(",") if args.leagues else list(LEAGUE_MAP.keys())
    run(leagues=leagues, days_ahead=args.days_ahead)


if __name__ == "__main__":
    main()
