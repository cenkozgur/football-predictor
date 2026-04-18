"""Bulk-ingest every European top flight for the last N seasons.

Main-format leagues are downloaded per season (season code like '2324').
New-format leagues are downloaded as one aggregated file per country, then
filtered to the matching calendar years.

Usage:
    python scripts/ingest_all.py                # default: 5 most recent seasons
    python scripts/ingest_all.py --seasons 3    # last 3 seasons only
    python scripts/ingest_all.py --only E0,D1   # subset
"""

from __future__ import annotations

import argparse
import time

from app.ingestion import football_data, football_data_new
from app.ingestion.leagues import (
    ALL_LEAGUES,
    BY_CODE,
    LeagueFormat,
    MAIN_LEAGUES,
    NEW_LEAGUES,
    recent_seasons,
)


def _season_codes_to_calendar_years(season_codes: list[str]) -> list[str]:
    """Convert main-format season codes like '2324' → ['2023', '2024']
    so we can filter the new-leagues aggregated CSVs to the same span.
    """
    years: set[str] = set()
    for code in season_codes:
        # code = 'SSEE' where SS = start year % 100, EE = end year % 100
        start_yy = int(code[:2])
        end_yy = int(code[2:])
        start_year = 2000 + start_yy
        end_year = 2000 + end_yy
        years.add(str(start_year))
        years.add(str(end_year))
    return sorted(years)


def main() -> None:
    parser = argparse.ArgumentParser(description="Bulk-ingest all European top flights.")
    parser.add_argument("--seasons", type=int, default=5, help="Number of recent seasons.")
    parser.add_argument(
        "--only",
        default=None,
        help="Comma-separated league codes to restrict to (e.g. E0,D1,SP1).",
    )
    parser.add_argument(
        "--skip",
        default=None,
        help="Comma-separated league codes to exclude.",
    )
    parser.add_argument(
        "--latest-end-year",
        type=int,
        default=2025,
        help="End year of the most recent season (e.g. 2025 → season 2425 is the newest).",
    )
    args = parser.parse_args()

    season_codes = recent_seasons(args.seasons, latest_end_year=args.latest_end_year)
    calendar_years = _season_codes_to_calendar_years(season_codes)

    only: set[str] | None = set(args.only.split(",")) if args.only else None
    skip: set[str] = set(args.skip.split(",")) if args.skip else set()

    def selected(code: str) -> bool:
        if only is not None and code not in only:
            return False
        if code in skip:
            return False
        return True

    print(f"Seasons (main format): {season_codes}")
    print(f"Years   (new  format): {calendar_years}")
    print()

    totals: dict[str, int] = {}
    t0 = time.time()

    for spec in MAIN_LEAGUES:
        if not selected(spec.code):
            continue
        league_total = 0
        for season in season_codes:
            league_total += football_data.ingest_season(spec.code, season)
        totals[spec.code] = league_total

    for spec in NEW_LEAGUES:
        if not selected(spec.code):
            continue
        totals[spec.code] = football_data_new.ingest_country(spec.code, seasons=calendar_years)

    elapsed = time.time() - t0
    print()
    print("=" * 50)
    print(f"Done in {elapsed:.1f}s. Total matches per league:")
    for code in sorted(totals):
        spec = BY_CODE[code]
        print(f"  {code:4s} {spec.country:12s} {spec.name:25s} {totals[code]:6d}")
    print(f"  {'total':4s} {sum(totals.values()):>6d}")


if __name__ == "__main__":
    main()
