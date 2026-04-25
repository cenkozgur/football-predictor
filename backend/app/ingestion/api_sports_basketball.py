"""api-sports basketball ingester for SportEvent.

Pulls upcoming games for NBA, EuroLeague, and Turkish Basketbol Süper Ligi
into the sport_events table. Read-only consumer is the Bugün Ne Var web
app via /sport-events?sport=basketball.

Free tier of api-sports basketball is 100 requests/day. We make 1 request
per league per run = 3/day if we restrict to these three leagues, well
inside budget. Request is a date-range games query that returns every
upcoming game in one call.

Usage:
    export API_SPORTS_KEY=...
    python -m app.ingestion.api_sports_basketball --days-ahead 14
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import select

from app.db import SessionLocal
from app.models.sport_event import SportEvent


API_BASE = "https://v1.basketball.api-sports.io"

# League IDs in api-basketball. The most stable way to get these is the
# /leagues endpoint, but for our three target competitions the IDs below
# have been verified manually (and would only break if api-sports
# renumbers, which they essentially never do).
#
# Each entry: (api-basketball league id, season label, our internal code,
# display name shown to users).
LEAGUES: list[tuple[int, str, str, str]] = [
    (12,  "2025-2026", "NBA",        "NBA"),
    (120, "2025-2026", "EuroLeague", "EuroLeague"),
    (28,  "2025-2026", "BSL",        "Türkiye Basketbol Süper Ligi"),
]


def _client(api_key: str) -> httpx.Client:
    # api-sports allows two auth schemes; the direct one (api-sports.io
    # domain) uses x-apisports-key. RapidAPI proxy uses x-rapidapi-key.
    return httpx.Client(
        base_url=API_BASE,
        headers={
            "x-apisports-key": api_key,
            "x-rapidapi-key": api_key,
            "Accept": "application/json",
        },
        timeout=30.0,
    )


def fetch_games(
    api_key: str,
    league_id: int,
    season: str,
    days_ahead: int,
) -> list[dict[str, Any]]:
    today = datetime.now(tz=timezone.utc).date()
    date_to = (today + timedelta(days=days_ahead)).isoformat()
    params = {
        "league": league_id,
        "season": season,
        "from": today.isoformat(),
        "to": date_to,
    }
    with _client(api_key) as c:
        r = c.get("/games", params=params)
        r.raise_for_status()
        body = r.json()
    # Surface upstream errors and metadata so debugging "0 games" is
    # not a guessing game in cron logs.
    errs = body.get("errors")
    if errs:
        print(f"    api-basketball errors for league={league_id} season={season}: {errs}")
    results = body.get("results")
    if results == 0:
        print(f"    api-basketball returned 0 results (params={params})")
    games = body.get("response", []) or []
    return games


def probe_known_league_seasons(api_key: str) -> None:
    """One-off helper: print the actual /leagues entries so we can fix
    LEAGUE constants if the IDs / season labels we hard-coded are wrong.
    Run via the workflow's manual dispatch when ingest returns 0 games."""
    with _client(api_key) as c:
        for q in ("NBA", "EuroLeague", "Turkish Basketball Super League", "Basketbol"):
            try:
                r = c.get("/leagues", params={"search": q})
                body = r.json()
                rows = body.get("response", []) or []
                print(f"  search='{q}' → {len(rows)} matches")
                for row in rows[:5]:
                    seasons = row.get("seasons", []) or []
                    season_labels = [s.get("season") for s in seasons[-3:]]
                    print(
                        f"    id={row.get('id')} name={row.get('name')!r} "
                        f"country={(row.get('country') or {}).get('name')!r} "
                        f"recent_seasons={season_labels}"
                    )
            except Exception as e:  # noqa: BLE001
                print(f"  probe '{q}' failed: {e}")


def upsert_event(session, ext_ref: str, fields: dict[str, Any]) -> str:
    """Insert or update by external_ref. Returns 'inserted' / 'updated' / 'skip'."""
    existing = session.scalar(
        select(SportEvent).where(SportEvent.external_ref == ext_ref)
    )
    if existing is None:
        ev = SportEvent(external_ref=ext_ref, **fields)
        session.add(ev)
        return "inserted"
    changed = False
    for k, v in fields.items():
        if getattr(existing, k) != v:
            setattr(existing, k, v)
            changed = True
    return "updated" if changed else "skip"


def parse_kickoff(raw: dict[str, Any]) -> datetime | None:
    """api-basketball gives `date` as ISO 8601 string with timezone."""
    iso = raw.get("date")
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None


# api-basketball status codes: NS = Not Started, FT = Finished,
# Q1/Q2/Q3/Q4/OT = live, Postp = Postponed, Cancl = Cancelled.
def normalize_status(short: str | None) -> str:
    if not short:
        return "scheduled"
    s = short.upper()
    if s in {"FT", "AOT"}:
        return "finished"
    if s in {"Q1", "Q2", "Q3", "Q4", "OT", "BT", "HT"}:
        return "in_play"
    if s in {"POSTP", "CANC", "ABD", "AWD"}:
        return "cancelled"
    return "scheduled"


def run(api_key: str, days_ahead: int) -> None:
    print(f"Fetching basketball games for {len(LEAGUES)} leagues over next {days_ahead} days…")

    total_inserted = 0
    total_updated = 0
    total_skipped = 0

    with SessionLocal() as session:
        for league_id, season, internal_code, display_name in LEAGUES:
            try:
                games = fetch_games(api_key, league_id, season, days_ahead)
            except httpx.HTTPStatusError as e:
                print(f"  [{internal_code}] HTTP {e.response.status_code}: {e.response.text[:200]}")
                continue
            except httpx.HTTPError as e:
                print(f"  [{internal_code}] network error: {e}")
                continue

            print(f"  [{internal_code}] fetched {len(games)} games")

            for g in games:
                gid = g.get("id")
                if gid is None:
                    continue
                kickoff = parse_kickoff(g)
                if kickoff is None:
                    continue
                teams = g.get("teams") or {}
                home = (teams.get("home") or {}).get("name")
                away = (teams.get("away") or {}).get("name")
                if not home or not away:
                    continue
                status = normalize_status((g.get("status") or {}).get("short"))
                country = (g.get("country") or {}).get("name")
                venue = (g.get("venue") or "")  # api-basketball keeps venue at top level for some leagues

                ext_ref = f"apisports:basketball:{gid}"
                fields = {
                    "sport": "basketball",
                    "league": internal_code,
                    "season": season,
                    "kickoff": kickoff,
                    "home_team": home,
                    "away_team": away,
                    "venue": venue or country,
                    "broadcaster": None,  # api-basketball doesn't expose TR broadcaster; left blank
                    "status": status,
                }
                outcome = upsert_event(session, ext_ref, fields)
                if outcome == "inserted":
                    total_inserted += 1
                elif outcome == "updated":
                    total_updated += 1
                else:
                    total_skipped += 1

        session.commit()

    print(
        f"Done. inserted={total_inserted} updated={total_updated} unchanged={total_skipped}"
    )


def main() -> None:
    p = argparse.ArgumentParser(description="api-sports basketball ingester")
    p.add_argument("--days-ahead", type=int, default=14)
    p.add_argument(
        "--probe", action="store_true",
        help="Skip ingest; print /leagues lookup so we can fix the ID constants.",
    )
    args = p.parse_args()

    api_key = os.environ.get("API_SPORTS_KEY") or os.environ.get("API_FOOTBALL_KEY")
    if not api_key:
        print("Set API_SPORTS_KEY (or API_FOOTBALL_KEY — same key for all api-sports sports).")
        sys.exit(1)

    if args.probe:
        probe_known_league_seasons(api_key)
        return

    run(api_key=api_key, days_ahead=args.days_ahead)


if __name__ == "__main__":
    main()
