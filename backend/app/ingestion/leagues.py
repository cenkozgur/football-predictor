"""Catalog of every league we ingest from football-data.co.uk.

football-data.co.uk publishes data in two formats:

1. **Main leagues** — per-season CSVs at
   https://www.football-data.co.uk/mmz4281/{season}/{code}.csv
   Rich schema: FTHG/FTAG/HTHG/HTAG plus odds from many bookmakers.

2. **New / extra leagues** — aggregated CSVs at
   https://www.football-data.co.uk/new/{code}.csv
   One file per country containing every season; simpler schema with
   Pinnacle + Max + Avg odds.

This module lists every top-flight European league we support, classified by
which format their data lives in.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class LeagueFormat(Enum):
    MAIN = "main"  # per-season CSVs, rich schema
    NEW = "new"  # aggregated CSVs, simple schema


@dataclass(frozen=True)
class LeagueSpec:
    code: str  # football-data.co.uk code (e.g. "E0", "AUT")
    name: str  # Turkish-friendly display name
    country: str
    format: LeagueFormat


# Top flight only. Expand later (Phase 5: UCL/UEL/UECL, World Cup).
MAIN_LEAGUES: tuple[LeagueSpec, ...] = (
    LeagueSpec("E0", "Premier League", "England", LeagueFormat.MAIN),
    LeagueSpec("D1", "Bundesliga", "Germany", LeagueFormat.MAIN),
    LeagueSpec("I1", "Serie A", "Italy", LeagueFormat.MAIN),
    LeagueSpec("SP1", "La Liga", "Spain", LeagueFormat.MAIN),
    LeagueSpec("F1", "Ligue 1", "France", LeagueFormat.MAIN),
    LeagueSpec("N1", "Eredivisie", "Netherlands", LeagueFormat.MAIN),
    LeagueSpec("B1", "Jupiler Pro League", "Belgium", LeagueFormat.MAIN),
    LeagueSpec("P1", "Primeira Liga", "Portugal", LeagueFormat.MAIN),
    LeagueSpec("T1", "Süper Lig", "Turkey", LeagueFormat.MAIN),
    LeagueSpec("G1", "Super League", "Greece", LeagueFormat.MAIN),
    LeagueSpec("SC0", "Premiership", "Scotland", LeagueFormat.MAIN),
)

NEW_LEAGUES: tuple[LeagueSpec, ...] = (
    LeagueSpec("AUT", "Bundesliga", "Austria", LeagueFormat.NEW),
    LeagueSpec("DNK", "Superliga", "Denmark", LeagueFormat.NEW),
    LeagueSpec("FIN", "Veikkausliiga", "Finland", LeagueFormat.NEW),
    LeagueSpec("IRL", "Premier Division", "Ireland", LeagueFormat.NEW),
    LeagueSpec("NOR", "Eliteserien", "Norway", LeagueFormat.NEW),
    LeagueSpec("POL", "Ekstraklasa", "Poland", LeagueFormat.NEW),
    LeagueSpec("ROU", "Liga I", "Romania", LeagueFormat.NEW),
    LeagueSpec("RUS", "Premier League", "Russia", LeagueFormat.NEW),
    LeagueSpec("SWE", "Allsvenskan", "Sweden", LeagueFormat.NEW),
    LeagueSpec("SWZ", "Super League", "Switzerland", LeagueFormat.NEW),
)

ALL_LEAGUES: tuple[LeagueSpec, ...] = MAIN_LEAGUES + NEW_LEAGUES

BY_CODE: dict[str, LeagueSpec] = {spec.code: spec for spec in ALL_LEAGUES}


def get(code: str) -> LeagueSpec:
    if code not in BY_CODE:
        raise KeyError(f"Unknown league code: {code}. Known: {sorted(BY_CODE)}")
    return BY_CODE[code]


# ---------- season helpers for the "main" format ----------


def recent_seasons(n: int = 5, latest_end_year: int = 2025) -> list[str]:
    """Return the n most recent season codes like ['2122', '2223', '2324', '2425'].

    A "season" 2324 means 2023-08 → 2024-05.
    """
    out: list[str] = []
    end = latest_end_year
    for _ in range(n):
        start = end - 1
        out.append(f"{start % 100:02d}{end % 100:02d}")
        end -= 1
    return list(reversed(out))
