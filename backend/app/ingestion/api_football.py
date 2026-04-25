"""api-football.com ingester for upcoming fixtures and pre-match odds.

Why this exists
---------------
football-data.co.uk is a historical archive — it does not publish future
fixtures. To show upcoming coupons in the UI we need a live fixture source,
and since we also want odds for value-bet detection we use api-football.com
which provides both in one subscription.

Key design choices
------------------
* **League ID mapping** is explicit (LEAGUE_MAP below). api-football assigns
  its own numeric IDs; we map each to one of our internal codes.
* **Team name matching** is the fragile part. api-football spells things
  differently than football-data.co.uk (e.g. "Manchester United" vs
  "Man United"). We try: (a) exact-match against the Team table, (b) a
  normalized match (lowercase, strip common prefixes/suffixes), (c) fuzzy
  match with high threshold. Anything that doesn't resolve is reported so
  we can add an alias manually.
* **Request budget**: free tier = 100 req/day. We try to stay under 10/day:
  one fixtures call per league per run, plus one odds call per run (bulk).

Usage
-----
    export API_FOOTBALL_KEY=your_key_here
    python -m app.ingestion.api_football --days-ahead 7
    python -m app.ingestion.api_football --leagues E0,SP1 --days-ahead 14
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
from app.models.odds import Odds
from app.models.team import Team


API_BASE = "https://v3.football.api-sports.io"


# Internal code → (api-football league_id, default season year)
# Season convention: 2025 = 2025/26 season for European leagues.
LEAGUE_MAP: dict[str, tuple[int, int]] = {
    "E0":  (39, 2025),   # Premier League
    "SP1": (140, 2025),  # La Liga
    "I1":  (135, 2025),  # Serie A
    "D1":  (78, 2025),   # Bundesliga
    "F1":  (61, 2025),   # Ligue 1
    "P1":  (94, 2025),   # Primeira Liga
    "N1":  (88, 2025),   # Eredivisie
    "B1":  (144, 2025),  # Jupiler Pro League
    "SC0": (179, 2025),  # Scottish Premiership
    "T1":  (203, 2025),  # Turkish Süper Lig
    "AUT": (218, 2025),  # Austrian Bundesliga
    "DNK": (119, 2025),  # Danish Superliga
    "G1":  (197, 2025),  # Greek Super League
    "NOR": (103, 2026),  # Eliteserien (calendar-year season)
    "POL": (106, 2025),  # Ekstraklasa
    "ROU": (283, 2025),  # Liga I
    "RUS": (235, 2025),  # Russian Premier League
    "SWE": (113, 2026),  # Allsvenskan (calendar-year season)
    "FIN": (244, 2026),  # Veikkausliiga (calendar-year season)
    "IRL": (357, 2026),  # Irish Premier Division (calendar-year season)
}


# Manual aliases for team names that don't match automatically.
# Map api-football spelling → name as stored in our Team table.
# Populate this as unmatched teams are reported by fetch_prematch_odds /
# fetch_xg_api_football. Goal: every Big-5 + commonly-played league team
# resolves on the first pass so the composer's odds coverage is full.
TEAM_ALIASES: dict[str, str] = {
    # Premier League (E0)
    "Manchester United": "Man United",
    "Manchester City": "Man City",
    "Tottenham": "Tottenham",
    "Nottingham Forest": "Nott'm Forest",
    "Newcastle": "Newcastle",
    "Wolverhampton Wanderers": "Wolves",
    "Sheffield Utd": "Sheffield United",

    # Ligue 1 (F1)
    "Paris Saint Germain": "Paris SG",

    # Bundesliga (D1)
    "Bayern München": "Bayern Munich",
    "Borussia Mönchengladbach": "M'gladbach",

    # La Liga (SP1) — api-football uses "Athletic Club" for Bilbao; the
    # other shortenings here are how football-data.co.uk's CSV (our
    # historical source) labels them, so our Team.name matches that style.
    "Athletic Club": "Ath Bilbao",
    "Atletico Madrid": "Ath Madrid",
    "Real Sociedad": "Sociedad",
    "Real Betis": "Betis",

    # Primeira Liga (P1)
    "Sporting CP": "Sp Lisbon",
    "Sporting Braga": "Sp Braga",
    "SC Braga": "Sp Braga",
    "Vitoria Guimaraes": "Guimaraes",
    "Vitória SC": "Guimaraes",

    # Eredivisie (N1) — common provider variants
    "PSV Eindhoven": "PSV Eindhoven",
    "Feyenoord": "Feyenoord",

    # Süper Lig (T1) — api-football sends Turkish names with diacritics +
    # full club suffixes; the Team table stores the ASCII / shortened
    # form football-data.co.uk uses, so the auto-normalizer misses these.
    # Without these aliases ~6 of every 17 weekly fixtures are silently
    # dropped (observed 2026-04-25: cost us Beşiktaş's match).
    "Beşiktaş": "Besiktas",
    "Galatasaray": "Galatasaray",
    "Fenerbahçe": "Fenerbahce",
    "Trabzonspor": "Trabzonspor",
    "Başakşehir FK": "Basaksehir",
    "Başakşehir": "Basaksehir",
    "İstanbul Başakşehir": "Basaksehir",
    "Fatih Karagümrük": "Karagumruk",
    "Karagümrük": "Karagumruk",
    "Gaziantep FK": "Gaziantep",
    "Gaziantep": "Gaziantep",
    "Gençlerbirliği S.K.": "Genclerbirligi",
    "Gençlerbirliği": "Genclerbirligi",
    "Göztepe": "Goztep",
    "Eyüpspor": "Eyupspor",
    "Kasımpaşa": "Kasimpasa",
    "Adana Demirspor": "Adana Demirspor",
    "Antalyaspor": "Antalyaspor",
    "Konyaspor": "Konyaspor",
    "Alanyaspor": "Alanyaspor",
    "Sivasspor": "Sivasspor",
    "Kayserispor": "Kayserispor",
    "Çaykur Rizespor": "Rizespor",
    "Rizespor": "Rizespor",
    "Samsunspor": "Samsunspor",
    "Kocaelispor": "Kocaelispor",
    "Bodrum FK": "Bodrum",
    "Bodrum": "Bodrum",
    "Pendikspor": "Pendikspor",
    "İstanbulspor": "Istanbulspor",
    "Hatayspor": "Hatayspor",
}


# --------------------------- HTTP ---------------------------


def _client(api_key: str) -> httpx.Client:
    return httpx.Client(
        base_url=API_BASE,
        headers={"x-apisports-key": api_key},
        timeout=30.0,
    )


def _get(client: httpx.Client, path: str, params: dict[str, Any]) -> dict[str, Any]:
    r = client.get(path, params=params)
    r.raise_for_status()
    data = r.json()
    if data.get("errors"):
        # api-football returns 200 with an errors object for auth/quota issues.
        raise RuntimeError(f"api-football error on {path}: {data['errors']}")
    return data


# --------------------------- Team matching ---------------------------


def _normalize(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"\b(fc|cf|afc|sc|ac|as|ss|club|de|el|la|le|the)\b", " ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _build_team_index(db) -> dict[str, Team]:
    """Return {normalized_name: Team} for every known team."""
    teams = list(db.scalars(select(Team)).all())
    index: dict[str, Team] = {}
    for t in teams:
        index[_normalize(t.name)] = t
    return index


def _resolve_team(
    name: str,
    team_index: dict[str, Team],
    fuzzy_cache: dict[str, Team | None],
) -> Team | None:
    # Try alias first
    if name in TEAM_ALIASES:
        norm = _normalize(TEAM_ALIASES[name])
        if norm in team_index:
            return team_index[norm]

    # Exact-normalized match
    norm = _normalize(name)
    if norm in team_index:
        return team_index[norm]

    # Fuzzy — high threshold, cached
    if name in fuzzy_cache:
        return fuzzy_cache[name]
    keys = list(team_index.keys())
    close = get_close_matches(norm, keys, n=1, cutoff=0.88)
    team = team_index[close[0]] if close else None
    fuzzy_cache[name] = team
    return team


# --------------------------- Ingest ---------------------------


def fetch_fixtures(
    api_key: str,
    leagues: list[str],
    days_ahead: int,
) -> list[dict[str, Any]]:
    """One API call per league. Returns raw fixture dicts."""
    date_from = datetime.now(tz=timezone.utc).date().isoformat()
    date_to = (datetime.now(tz=timezone.utc).date() + timedelta(days=days_ahead)).isoformat()
    fixtures: list[dict[str, Any]] = []
    with _client(api_key) as client:
        for code in leagues:
            if code not in LEAGUE_MAP:
                print(f"  [{code}] not in LEAGUE_MAP — skipping")
                continue
            api_id, season = LEAGUE_MAP[code]
            data = _get(client, "/fixtures", {
                "league": api_id, "season": season,
                "from": date_from, "to": date_to,
            })
            rows = data.get("response", [])
            print(f"  [{code}] fixtures {date_from}..{date_to}: {len(rows)}")
            for row in rows:
                row["_our_league_code"] = code
                row["_our_season"] = str(season)
                fixtures.append(row)
    return fixtures


def fetch_odds_for_fixtures(
    api_key: str,
    fixture_ids: list[int],
) -> dict[int, list[dict[str, Any]]]:
    """Returns {fixture_id: [bookmakers]}.

    api-football /odds endpoint returns odds per bookmaker per market. We
    batch by fixture id (one request returns up to 10 pages; free tier
    usually gives a handful of bookmakers per match)."""
    out: dict[int, list[dict[str, Any]]] = {}
    if not fixture_ids:
        return out
    with _client(api_key) as client:
        for fid in fixture_ids:
            try:
                data = _get(client, "/odds", {"fixture": fid})
            except Exception as exc:  # noqa: BLE001
                print(f"  odds fetch failed for fixture {fid}: {exc}")
                continue
            rows = data.get("response", [])
            if rows:
                out[fid] = rows[0].get("bookmakers", [])
            time.sleep(0.1)  # be polite — free tier has per-minute cap too
    return out


def upsert_fixtures(db, fixtures: list[dict[str, Any]]) -> tuple[int, int, list[str]]:
    """Insert/update Match rows. Returns (written, skipped, unmatched_teams)."""
    team_index = _build_team_index(db)
    fuzzy_cache: dict[str, Team | None] = {}
    written = 0
    skipped = 0
    unmatched: set[str] = set()

    for fx in fixtures:
        home_name = fx["teams"]["home"]["name"]
        away_name = fx["teams"]["away"]["name"]
        home = _resolve_team(home_name, team_index, fuzzy_cache)
        away = _resolve_team(away_name, team_index, fuzzy_cache)
        if home is None:
            unmatched.add(home_name)
        if away is None:
            unmatched.add(away_name)
        if home is None or away is None:
            skipped += 1
            continue

        kickoff_iso = fx["fixture"]["date"]
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


def upsert_odds(db, odds_by_fixture: dict[int, list[dict[str, Any]]],
                 fixtures: list[dict[str, Any]]) -> int:
    """Convert api-football odds rows into our Odds table.

    We pick the first bookmaker returned (usually the highest-data one) and
    flatten their markets into (market, selection, decimal_odds) tuples."""
    if not odds_by_fixture:
        return 0

    # Build fixture_id → Match mapping by kickoff + teams (we already upserted)
    team_index = _build_team_index(db)
    fuzzy_cache: dict[str, Team | None] = {}
    written = 0

    # Market names differ between bookmakers; normalize the common ones.
    MARKET_MAP = {
        "Match Winner": "1X2",
        "Goals Over/Under": "OU",  # will suffix line
        "Both Teams Score": "BTTS",
        "Double Chance": "DC",
        "Exact Score": "CS",
    }
    SEL_MAP_1X2 = {"Home": "1", "Draw": "X", "Away": "2"}
    SEL_MAP_BTTS = {"Yes": "yes", "No": "no"}
    SEL_MAP_DC = {"Home/Draw": "1X", "Home/Away": "12", "Draw/Away": "X2"}

    for fx in fixtures:
        fid = fx["fixture"]["id"]
        bookmakers = odds_by_fixture.get(fid, [])
        if not bookmakers:
            continue
        home = _resolve_team(fx["teams"]["home"]["name"], team_index, fuzzy_cache)
        away = _resolve_team(fx["teams"]["away"]["name"], team_index, fuzzy_cache)
        if home is None or away is None:
            continue
        kickoff_iso = fx["fixture"]["date"]
        kickoff = datetime.fromisoformat(kickoff_iso.replace("Z", "+00:00"))
        kickoff_naive = kickoff.astimezone(timezone.utc).replace(tzinfo=None)
        match = db.execute(
            select(Match).where(
                Match.home_team_id == home.id,
                Match.away_team_id == away.id,
                Match.kickoff == kickoff_naive,
            )
        ).scalar_one_or_none()
        if match is None:
            continue

        book = bookmakers[0]  # pick first — usually best coverage
        source = book.get("name", "api-football")
        for bet in book.get("bets", []):
            name = bet.get("name")
            if name not in MARKET_MAP:
                continue
            base_market = MARKET_MAP[name]
            for v in bet.get("values", []):
                sel_raw = v.get("value")
                try:
                    odd = float(v.get("odd"))
                except (TypeError, ValueError):
                    continue

                if base_market == "1X2":
                    sel = SEL_MAP_1X2.get(sel_raw)
                    market = "1X2"
                elif base_market == "BTTS":
                    sel = SEL_MAP_BTTS.get(sel_raw)
                    market = "BTTS"
                elif base_market == "DC":
                    sel = SEL_MAP_DC.get(sel_raw)
                    market = "DC"
                elif base_market == "OU":
                    # "Over 2.5" / "Under 2.5"
                    parts = sel_raw.split() if sel_raw else []
                    if len(parts) != 2:
                        continue
                    side, line = parts
                    if side not in {"Over", "Under"}:
                        continue
                    sel = side.lower()
                    market = f"OU_{line}"
                elif base_market == "CS":
                    sel = sel_raw  # "2-1"
                    market = "CS"
                else:
                    continue
                if sel is None:
                    continue

                db.add(Odds(
                    match_id=match.id,
                    source=source,
                    market=market,
                    selection=sel,
                    decimal_odds=odd,
                ))
                written += 1
    db.commit()
    return written


# --------------------------- CLI ---------------------------


def run(leagues: list[str], days_ahead: int, fetch_odds: bool) -> None:
    api_key = os.environ.get("API_FOOTBALL_KEY")
    if not api_key:
        print("Set API_FOOTBALL_KEY environment variable.")
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

        if fetch_odds and fixtures:
            # Only fetch odds for fixtures we actually stored
            stored_ids = []
            team_index = _build_team_index(db)
            fuzzy_cache: dict[str, Team | None] = {}
            for fx in fixtures:
                home = _resolve_team(fx["teams"]["home"]["name"], team_index, fuzzy_cache)
                away = _resolve_team(fx["teams"]["away"]["name"], team_index, fuzzy_cache)
                if home and away:
                    stored_ids.append(fx["fixture"]["id"])
            print(f"\nFetching odds for {len(stored_ids)} fixtures…")
            odds = fetch_odds_for_fixtures(api_key, stored_ids)
            n_odds = upsert_odds(db, odds, fixtures)
            print(f"Inserted {n_odds} odds rows.")


def main() -> None:
    p = argparse.ArgumentParser(description="api-football ingester")
    p.add_argument("--leagues", default=None,
                   help="Comma-separated internal codes. Default: all mapped.")
    p.add_argument("--days-ahead", type=int, default=7)
    p.add_argument("--no-odds", action="store_true",
                   help="Skip odds fetching (fixtures only — saves API calls).")
    args = p.parse_args()
    leagues = args.leagues.split(",") if args.leagues else list(LEAGUE_MAP.keys())
    run(leagues=leagues, days_ahead=args.days_ahead, fetch_odds=not args.no_odds)


if __name__ == "__main__":
    main()
