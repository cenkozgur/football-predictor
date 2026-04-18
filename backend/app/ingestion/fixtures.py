"""Ingester for upcoming (unplayed) fixtures from football-data.co.uk.

Unlike the historical ingesters which read one league-season at a time, the
fixtures publication is a single file per format:

- Main-format leagues:   https://www.football-data.co.uk/fixtures.csv
- New-format leagues:    https://www.football-data.co.uk/new_league_fixtures.csv

Both files carry *only* upcoming matches (no FTHG/FTAG yet), with the same
column layout as their historical cousins. We filter to the leagues we care
about via the league catalog, then upsert Match rows with status="scheduled".

Why a separate module
---------------------
The historical ingesters in football_data.py / football_data_new.py are opinionated
about having a final score — they skip rows where FTHG/FTAG is NaN, which is
exactly what an unplayed fixture looks like. Splitting fixtures into its own
module keeps that invariant intact and lets us be explicit about upcoming-only
semantics (no odds ingestion here, no result updates here).

Usage:
    python -m app.ingestion.fixtures                    # both formats, all known leagues
    python -m app.ingestion.fixtures --format main      # only main-format leagues
    python -m app.ingestion.fixtures --format new       # only new-format leagues
    python -m app.ingestion.fixtures --leagues E0,D1    # restrict by code
"""

from __future__ import annotations

import argparse
import io
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal, init_db
from app.ingestion.base import HttpFetcher, proxied, strip_jina_header
from app.ingestion.leagues import BY_CODE, MAIN_LEAGUES, NEW_LEAGUES, LeagueFormat
from app.models.match import Match
from app.models.team import Team

MAIN_FIXTURES_URL = "https://www.football-data.co.uk/fixtures.csv"
NEW_FIXTURES_URL = "https://www.football-data.co.uk/new_league_fixtures.csv"


def _get_or_create_team(
    db: Session, name: str, country: str, league_name: str
) -> Team:
    name = name.strip()
    existing = db.scalar(
        select(Team).where(Team.name == name, Team.country == country)
    )
    if existing:
        return existing
    team = Team(name=name, country=country, league=league_name)
    db.add(team)
    db.flush()
    return team


def _parse_date(date_str: str) -> datetime | None:
    """Accept every date format football-data.co.uk files use.

    Main format:  DD/MM/YY or DD/MM/YYYY
    New format:   YYYY-MM-DD or DD/MM/YYYY
    """
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return None


def _parse_kickoff(date_str: str, time_str: str | float | None) -> datetime | None:
    dt = _parse_date(date_str)
    if dt is None:
        return None
    if isinstance(time_str, str) and ":" in time_str:
        try:
            hh, mm = time_str.split(":")
            dt = dt.replace(hour=int(hh), minute=int(mm))
        except ValueError:
            pass
    return dt.replace(tzinfo=timezone.utc)


def _fetch_csv(url: str) -> pd.DataFrame | None:
    settings = get_settings()
    fetcher = HttpFetcher()
    resolved = proxied(url, settings.football_data_proxy)
    print(f"[fixtures] GET {resolved}")
    try:
        response = fetcher.get(resolved)
    except Exception as exc:
        print(f"[fixtures] fetch failed: {exc}")
        return None
    finally:
        fetcher.close()

    body = strip_jina_header(response.text)
    try:
        df = pd.read_csv(
            io.StringIO(body), encoding="latin-1", on_bad_lines="skip"
        )
    except Exception as exc:
        print(f"[fixtures] parse failed: {exc}")
        return None
    # football-data.co.uk files sometimes have trailing empty rows; drop them.
    df = df.dropna(how="all")
    return df


def _upsert_fixture(
    db: Session,
    *,
    league_code: str,
    league_name: str,
    country: str,
    home_name: str,
    away_name: str,
    kickoff: datetime,
    season: str,
) -> str:
    """Insert or refresh a scheduled Match. Returns 'inserted' | 'updated' | 'skipped'."""
    home = _get_or_create_team(db, home_name, country, league_name)
    away = _get_or_create_team(db, away_name, country, league_name)

    existing = db.scalar(
        select(Match).where(
            Match.home_team_id == home.id,
            Match.away_team_id == away.id,
            Match.kickoff == kickoff,
        )
    )
    if existing is not None:
        # Don't stomp finished rows with a scheduled downgrade — if the fixture
        # is still in the upcoming file after the match was played and ingested,
        # that's almost certainly the same match and we should leave it alone.
        if existing.status == "finished":
            return "skipped"
        existing.season = season
        return "updated"

    db.add(
        Match(
            league=league_code,
            season=season,
            kickoff=kickoff,
            home_team_id=home.id,
            away_team_id=away.id,
            ft_home=None,
            ft_away=None,
            ht_home=None,
            ht_away=None,
            status="scheduled",
        )
    )
    return "inserted"


def ingest_main_fixtures(league_codes: set[str] | None = None) -> int:
    """Ingest upcoming fixtures for main-format leagues (England, Germany, ...)."""
    df = _fetch_csv(MAIN_FIXTURES_URL)
    if df is None or df.empty:
        return 0

    required = {"Div", "Date", "HomeTeam", "AwayTeam"}
    missing = required - set(df.columns)
    if missing:
        print(f"[fixtures/main] CSV missing {missing}; skipping")
        return 0

    known_main = {s.code for s in MAIN_LEAGUES}
    target_codes = (
        (league_codes & known_main) if league_codes is not None else known_main
    )

    n_inserted = 0
    n_updated = 0
    n_skipped = 0
    with SessionLocal() as db:
        for _, row in df.iterrows():
            div = str(row.get("Div", "")).strip()
            if div not in target_codes:
                continue
            spec = BY_CODE[div]
            if pd.isna(row.get("HomeTeam")) or pd.isna(row.get("AwayTeam")):
                continue
            kickoff = _parse_kickoff(str(row["Date"]), row.get("Time"))
            if kickoff is None:
                continue

            # Season is not carried in fixtures.csv; derive from kickoff month.
            # Aug–Dec → start/end = (year, year+1); Jan–Jul → (year-1, year).
            yr = kickoff.year
            if kickoff.month >= 8:
                season = f"{yr % 100:02d}{(yr + 1) % 100:02d}"
            else:
                season = f"{(yr - 1) % 100:02d}{yr % 100:02d}"

            outcome = _upsert_fixture(
                db,
                league_code=div,
                league_name=spec.name,
                country=spec.country,
                home_name=str(row["HomeTeam"]),
                away_name=str(row["AwayTeam"]),
                kickoff=kickoff,
                season=season,
            )
            if outcome == "inserted":
                n_inserted += 1
            elif outcome == "updated":
                n_updated += 1
            else:
                n_skipped += 1
        db.commit()
    print(
        f"[fixtures/main] inserted={n_inserted} updated={n_updated} "
        f"skipped={n_skipped}"
    )
    return n_inserted + n_updated


def ingest_new_fixtures(league_codes: set[str] | None = None) -> int:
    """Ingest upcoming fixtures for new-format leagues (Austria, Sweden, ...).

    The new_league_fixtures.csv schema is close to the historical new-format
    schema: Country, League, Season, Date, [Time,] Home, Away, HG, AG, ...
    We identify leagues by Country (since the file has no League *code* column).
    """
    df = _fetch_csv(NEW_FIXTURES_URL)
    if df is None or df.empty:
        return 0

    required = {"Country", "Date", "Home", "Away"}
    missing = required - set(df.columns)
    if missing:
        print(f"[fixtures/new] CSV missing {missing}; skipping")
        return 0

    known_new = {s.code for s in NEW_LEAGUES}
    target_codes = (
        (league_codes & known_new) if league_codes is not None else known_new
    )

    country_to_code = {
        spec.country.lower(): spec.code for spec in NEW_LEAGUES
    }

    n_inserted = 0
    n_updated = 0
    n_skipped = 0
    with SessionLocal() as db:
        for _, row in df.iterrows():
            country_raw = str(row.get("Country", "")).strip()
            code = country_to_code.get(country_raw.lower())
            if code is None or code not in target_codes:
                continue
            spec = BY_CODE[code]
            if pd.isna(row.get("Home")) or pd.isna(row.get("Away")):
                continue
            kickoff = _parse_kickoff(str(row["Date"]), row.get("Time"))
            if kickoff is None:
                continue

            season = (
                str(row["Season"]) if "Season" in row.index and not pd.isna(row.get("Season")) else str(kickoff.year)
            )

            outcome = _upsert_fixture(
                db,
                league_code=code,
                league_name=spec.name,
                country=spec.country,
                home_name=str(row["Home"]),
                away_name=str(row["Away"]),
                kickoff=kickoff,
                season=season,
            )
            if outcome == "inserted":
                n_inserted += 1
            elif outcome == "updated":
                n_updated += 1
            else:
                n_skipped += 1
        db.commit()
    print(
        f"[fixtures/new] inserted={n_inserted} updated={n_updated} "
        f"skipped={n_skipped}"
    )
    return n_inserted + n_updated


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest upcoming fixtures from football-data.co.uk."
    )
    parser.add_argument(
        "--format",
        choices=("main", "new", "both"),
        default="both",
        help="Which format's fixtures file to fetch (default: both).",
    )
    parser.add_argument(
        "--leagues",
        default=None,
        help="Comma-separated league codes to restrict to.",
    )
    args = parser.parse_args()

    init_db()

    league_codes = (
        set(args.leagues.split(",")) if args.leagues else None
    )
    total = 0
    if args.format in ("main", "both"):
        total += ingest_main_fixtures(league_codes)
    if args.format in ("new", "both"):
        total += ingest_new_fixtures(league_codes)
    print(f"[fixtures] total rows processed: {total}")


if __name__ == "__main__":
    main()
