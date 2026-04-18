"""Ingester for football-data.co.uk *new leagues* aggregated CSVs.

URL pattern:
    https://www.football-data.co.uk/new/{code}.csv

One file per country, containing every season available. Simpler schema:

    Country, League, Season, Date, [Time,] Home, Away, HG, AG, Res,
    PH, PD, PA, MaxH, MaxD, MaxA, AvgH, AvgD, AvgA, ...

- HG / AG are the full-time goals (no half-time breakdown in this format).
- Dates are typically YYYY-MM-DD but DD/MM/YYYY also occurs — we handle both.
- Pinnacle odds (PH/PD/PA) are opening; some newer files add PCH/PCD/PCA closing.
- Summer leagues (Sweden, Norway, Finland, Ireland) use calendar-year season codes.

Usage:
    python -m app.ingestion.football_data_new --league SWE
    python -m app.ingestion.football_data_new --league AUT --seasons 2023,2024
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

BASE_URL = "https://www.football-data.co.uk/new/{code}.csv"


# The new-format files use the suffix "C" to denote *closing* odds, and the
# current files only carry closing lines (PSCH/PSCD/PSCA = Pinnacle closing,
# B365CH/B365CD/B365CA = Bet365 closing, AvgCH/AvgCD/AvgCA = average closing,
# MaxCH/MaxCD/MaxCA = max/best closing, BFECH/BFECD/BFECA = Betfair Exchange
# closing). There are no opening-odds columns in new-format files — backtests
# that bet against "opening" will fall through to these same closing sources.
ONE_X_TWO_SOURCES: list[tuple[str, tuple[str, str, str]]] = [
    ("PSC", ("PSCH", "PSCD", "PSCA")),           # Pinnacle closing
    ("B365C", ("B365CH", "B365CD", "B365CA")),   # Bet365 closing
    ("BFEC", ("BFECH", "BFECD", "BFECA")),       # Betfair Exchange closing
    ("AvgC", ("AvgCH", "AvgCD", "AvgCA")),       # Average closing
    ("MaxC", ("MaxCH", "MaxCD", "MaxCA")),       # Max closing
]


def _parse_kickoff(date_str: str, time_str: str | float | None) -> datetime:
    """New-leagues format dates are usually YYYY-MM-DD but can be DD/MM/YYYY."""
    dt: datetime | None = None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y"):
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


def _get_or_create_team(db: Session, name: str, country: str, league_name: str) -> Team:
    name = name.strip()
    existing = db.scalar(select(Team).where(Team.name == name, Team.country == country))
    if existing:
        return existing
    team = Team(name=name, country=country, league=league_name)
    db.add(team)
    db.flush()
    return team


def _extract_1x2_odds(row: pd.Series, match_id: int, db: Session) -> int:
    """Insert 1X2 odds from every available source so the backtester can pick."""
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


def ingest_country(league_code: str, seasons: list[str] | None = None) -> int:
    """Download one country's aggregated CSV and upsert matches + odds.

    Parameters
    ----------
    league_code : str
        Country code from the NEW_LEAGUES catalog (e.g. "SWE", "AUT").
    seasons : list[str] | None
        Optional filter — only keep rows whose Season column is in this list.
        If None, ingest every season in the file.
    """
    spec = get_league(league_code)
    if spec.format is not LeagueFormat.NEW:
        raise ValueError(
            f"{league_code} is a '{spec.format.value}' league — use "
            "app.ingestion.football_data instead."
        )

    init_db()

    settings = get_settings()
    fetcher = HttpFetcher()
    raw_url = BASE_URL.format(code=league_code)
    url = proxied(raw_url, settings.football_data_proxy)
    print(f"[{league_code}] GET {url}")
    try:
        response = fetcher.get(url)
    except Exception as exc:
        print(f"[{league_code}] fetch failed: {exc}")
        fetcher.close()
        return 0
    fetcher.close()

    body = strip_jina_header(response.text)

    df = pd.read_csv(io.StringIO(body), encoding="latin-1", on_bad_lines="skip")
    required = {"Date", "Home", "Away", "HG", "AG"}
    missing = required - set(df.columns)
    if missing:
        print(f"[{league_code}] CSV missing {missing}; skipping")
        return 0

    if seasons is not None and "Season" in df.columns:
        # Season values come in two flavours:
        #   - summer leagues (SWE, NOR, FIN, IRL): "2023"
        #   - winter leagues (AUT, POL, DNK, SWZ, RUS, ROU): "2023/2024"
        # We accept a row if any target year appears as a substring of Season.
        season_str = df["Season"].astype(str)
        mask = season_str.apply(lambda s: any(y in s for y in seasons))
        df = df[mask]

    n_matches = 0
    n_odds = 0
    with SessionLocal() as db:
        for _, row in df.iterrows():
            if pd.isna(row["Home"]) or pd.isna(row["Away"]):
                continue
            if pd.isna(row["HG"]) or pd.isna(row["AG"]):
                continue

            season_code = str(row["Season"]) if "Season" in row.index else "unknown"

            home = _get_or_create_team(db, str(row["Home"]), spec.country, spec.name)
            away = _get_or_create_team(db, str(row["Away"]), spec.country, spec.name)

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
                    season=season_code,
                    kickoff=kickoff,
                    home_team_id=home.id,
                    away_team_id=away.id,
                    ft_home=int(row["HG"]),
                    ft_away=int(row["AG"]),
                    ht_home=None,
                    ht_away=None,
                    status="finished",
                )
                db.add(match)
                db.flush()
                match_id = match.id
            else:
                existing.ft_home = int(row["HG"])
                existing.ft_away = int(row["AG"])
                existing.status = "finished"
                match_id = existing.id
                db.query(Odds).filter(Odds.match_id == match_id).delete()

            n_odds += _extract_1x2_odds(row, match_id, db)
            n_matches += 1

        db.commit()
    print(f"[{league_code}] {n_matches} matches, {n_odds} odds rows")
    return n_matches


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest a football-data.co.uk new-leagues CSV.")
    parser.add_argument("--league", required=True, help="Country code, e.g. SWE, AUT")
    parser.add_argument(
        "--seasons",
        default=None,
        help="Comma-separated Season values to keep, e.g. '2023,2024'. Default: all.",
    )
    args = parser.parse_args()
    seasons = args.seasons.split(",") if args.seasons else None
    ingest_country(args.league, seasons)


if __name__ == "__main__":
    main()
