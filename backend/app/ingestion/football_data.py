"""Ingester for football-data.co.uk *main* per-season CSVs.

URL pattern:
    https://www.football-data.co.uk/mmz4281/{season}/{code}.csv

Schema (the important columns we use):
    Date, Time, HomeTeam, AwayTeam, FTHG, FTAG, FTR, HTHG, HTAG, HTR
    B365H, B365D, B365A            — Bet365 opening 1X2
    B365CH, B365CD, B365CA         — Bet365 closing 1X2 (preferred for backtest)
    PSH, PSD, PSA                  — Pinnacle opening 1X2
    PSCH, PSCD, PSCA               — Pinnacle closing 1X2
    B365>2.5, B365<2.5             — Bet365 Over/Under 2.5 (opening)
    B365C>2.5, B365C<2.5           — Bet365 Over/Under 2.5 (closing)

Usage:
    python -m app.ingestion.football_data --league E0 --season 2324
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
from app.ingestion.leagues import LeagueFormat, get as get_league
from app.models.match import Match
from app.models.odds import Odds
from app.models.team import Team

BASE_URL = "https://www.football-data.co.uk/mmz4281/{season}/{code}.csv"


# Source preference order: closing before opening, since closing odds embed the most
# information the market had. The first source whose columns are all present wins.
ONE_X_TWO_SOURCES: list[tuple[str, tuple[str, str, str]]] = [
    ("B365C", ("B365CH", "B365CD", "B365CA")),
    ("PSC", ("PSCH", "PSCD", "PSCA")),
    ("B365", ("B365H", "B365D", "B365A")),
    ("PS", ("PSH", "PSD", "PSA")),
    ("WH", ("WHH", "WHD", "WHA")),
]

OU_25_SOURCES: list[tuple[str, tuple[str, str]]] = [
    ("B365C", ("B365C>2.5", "B365C<2.5")),
    ("B365", ("B365>2.5", "B365<2.5")),
    ("P", ("P>2.5", "P<2.5")),
]


def _get_or_create_team(db: Session, name: str, country: str, league_name: str) -> Team:
    name = name.strip()
    existing = db.scalar(select(Team).where(Team.name == name, Team.country == country))
    if existing:
        return existing
    team = Team(name=name, country=country, league=league_name)
    db.add(team)
    db.flush()
    return team


def _parse_kickoff(date_str: str, time_str: str | float | None) -> datetime:
    """football-data.co.uk main-format dates are DD/MM/YY or DD/MM/YYYY."""
    dt: datetime | None = None
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            dt = datetime.strptime(date_str, fmt)
            break
        except ValueError:
            continue
    if dt is None:
        raise ValueError(f"Unrecognized date format: {date_str}")

    if isinstance(time_str, str) and ":" in time_str:
        try:
            hh, mm = time_str.split(":")
            dt = dt.replace(hour=int(hh), minute=int(mm))
        except ValueError:
            pass
    return dt.replace(tzinfo=timezone.utc)


def _extract_1x2_odds(row: pd.Series, match_id: int, db: Session) -> int:
    """Insert 1X2 odds rows for this match from EVERY available bookmaker source.

    We deliberately store all sources (not just the highest-priority one) so that
    downstream code — backtests in particular — can choose between opening odds
    (B365 / PS / WH) and closing odds (B365C / PSC). The backtester picks per run.

    Returns the number of Odds rows inserted.
    """
    inserted = 0
    for source, (h, d, a) in ONE_X_TWO_SOURCES:
        if h not in row.index or d not in row.index or a not in row.index:
            continue
        if pd.isna(row[h]) or pd.isna(row[d]) or pd.isna(row[a]):
            continue
        db.add_all(
            [
                Odds(match_id=match_id, source=source, market="1X2", selection="1", decimal_odds=float(row[h])),
                Odds(match_id=match_id, source=source, market="1X2", selection="X", decimal_odds=float(row[d])),
                Odds(match_id=match_id, source=source, market="1X2", selection="2", decimal_odds=float(row[a])),
            ]
        )
        inserted += 3
    return inserted


def _extract_ou25_odds(row: pd.Series, match_id: int, db: Session) -> int:
    inserted = 0
    for source, (over_col, under_col) in OU_25_SOURCES:
        if over_col not in row.index or under_col not in row.index:
            continue
        if pd.isna(row[over_col]) or pd.isna(row[under_col]):
            continue
        db.add_all(
            [
                Odds(match_id=match_id, source=source, market="OU_2.5", selection="over", decimal_odds=float(row[over_col])),
                Odds(match_id=match_id, source=source, market="OU_2.5", selection="under", decimal_odds=float(row[under_col])),
            ]
        )
        inserted += 2
    return inserted


def ingest_season(league_code: str, season: str) -> int:
    """Download one league-season CSV and upsert matches + odds into the DB.

    Returns the number of matches processed.
    """
    spec = get_league(league_code)
    if spec.format is not LeagueFormat.MAIN:
        raise ValueError(
            f"{league_code} is a '{spec.format.value}' league — use "
            "app.ingestion.football_data_new instead."
        )

    init_db()

    settings = get_settings()
    fetcher = HttpFetcher()
    raw_url = BASE_URL.format(season=season, code=league_code)
    url = proxied(raw_url, settings.football_data_proxy)
    print(f"[{league_code} {season}] GET {url}")
    try:
        response = fetcher.get(url)
    except Exception as exc:
        print(f"[{league_code} {season}] fetch failed: {exc}")
        fetcher.close()
        return 0
    fetcher.close()

    # When fetched via Jina Reader, strip its markdown header wrapper.
    body = strip_jina_header(response.text)

    # football-data.co.uk CSVs are latin-1 encoded with inconsistent trailing commas
    df = pd.read_csv(io.StringIO(body), encoding="latin-1", on_bad_lines="skip")
    required = {"Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG"}
    missing = required - set(df.columns)
    if missing:
        print(f"[{league_code} {season}] CSV missing {missing}; skipping")
        return 0

    n_matches = 0
    n_odds = 0
    with SessionLocal() as db:
        for _, row in df.iterrows():
            if pd.isna(row["HomeTeam"]) or pd.isna(row["AwayTeam"]):
                continue
            if pd.isna(row["FTHG"]) or pd.isna(row["FTAG"]):
                continue

            home = _get_or_create_team(db, str(row["HomeTeam"]), spec.country, spec.name)
            away = _get_or_create_team(db, str(row["AwayTeam"]), spec.country, spec.name)

            try:
                kickoff = _parse_kickoff(str(row["Date"]), row.get("Time"))
            except ValueError:
                continue

            existing = db.scalar(
                select(Match).where(
                    Match.home_team_id == home.id,
                    Match.away_team_id == away.id,
                    Match.kickoff == kickoff,
                )
            )
            if existing is None:
                match = Match(
                    league=league_code,
                    season=season,
                    kickoff=kickoff,
                    home_team_id=home.id,
                    away_team_id=away.id,
                    ft_home=int(row["FTHG"]),
                    ft_away=int(row["FTAG"]),
                    ht_home=int(row["HTHG"]) if not pd.isna(row.get("HTHG")) else None,
                    ht_away=int(row["HTAG"]) if not pd.isna(row.get("HTAG")) else None,
                    status="finished",
                )
                db.add(match)
                db.flush()
                match_id = match.id
            else:
                existing.ft_home = int(row["FTHG"])
                existing.ft_away = int(row["FTAG"])
                existing.status = "finished"
                match_id = existing.id
                # Clear prior odds from this source preference; we'll re-insert below.
                db.query(Odds).filter(Odds.match_id == match_id).delete()

            n_odds += _extract_1x2_odds(row, match_id, db)
            n_odds += _extract_ou25_odds(row, match_id, db)
            n_matches += 1

        db.commit()
    print(f"[{league_code} {season}] {n_matches} matches, {n_odds} odds rows")
    return n_matches


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest a football-data.co.uk main-format season.")
    parser.add_argument("--league", required=True, help="League code, e.g. E0, D1, T1")
    parser.add_argument("--season", required=True, help='Season code, e.g. "2324"')
    args = parser.parse_args()
    ingest_season(args.league, args.season)


if __name__ == "__main__":
    main()
