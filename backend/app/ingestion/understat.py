"""Understat xG ingester.

Scrapes Understat's season/league pages, extracts per-match xG via the
JSON-embedded `datesData` variable, and writes `xg_home` / `xg_away` onto
existing matches in our DB.

Only 5 of our 8 leagues are covered:
    EPL (E0), La Liga (SP1), Bundesliga (D1), Serie A (I1), Ligue 1 (F1)

The other 3 (Championship E1, Eredivisie N1, Primeira P1) are not on
Understat and will silently have no xG — the model treats them via the
goals-only fallback path.

Understat blocks Turkish IPs (returns a stripped-down 18KB page). Run
this from GitHub Actions or a non-TR proxy.

Team-name matching reuses the same normalization approach as
football_data_org.py: NFKD strip + common-word removal + explicit alias
table for the stubborn cases.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db import SessionLocal
from app.models.match import Match
from app.models.team import Team


# Understat league slug → our internal code
LEAGUE_MAP = {
    "EPL": "E0",
    "La_liga": "SP1",
    "Bundesliga": "D1",
    "Serie_A": "I1",
    "Ligue_1": "F1",
}

# Understat names that don't normalize cleanly to ours.
TEAM_ALIASES: dict[str, str] = {
    # EPL
    "Manchester City": "Man City",
    "Manchester United": "Man United",
    "Newcastle United": "Newcastle",
    "Tottenham": "Tottenham",
    "West Ham": "West Ham",
    "Wolverhampton Wanderers": "Wolves",
    "Nottingham Forest": "Nott'm Forest",
    "Leicester": "Leicester",
    "Leeds": "Leeds",
    "Ipswich": "Ipswich",
    # La Liga
    "Atletico Madrid": "Ath Madrid",
    "Athletic Club": "Ath Bilbao",
    "Real Sociedad": "Sociedad",
    "Real Betis": "Betis",
    "Real Valladolid": "Valladolid",
    "Celta Vigo": "Celta",
    "Rayo Vallecano": "Vallecano",
    "Cadiz": "Cadiz",
    "Espanyol": "Espanol",
    "Almeria": "Almeria",
    "Alaves": "Alaves",
    "Las Palmas": "Las Palmas",
    # Bundesliga
    "Bayern Munich": "Bayern Munich",
    "Borussia Dortmund": "Dortmund",
    "Borussia M.Gladbach": "M'gladbach",
    "Eintracht Frankfurt": "Ein Frankfurt",
    "Bayer Leverkusen": "Leverkusen",
    "RasenBallsport Leipzig": "RB Leipzig",
    "VfB Stuttgart": "Stuttgart",
    "VfL Wolfsburg": "Wolfsburg",
    "VfL Bochum": "Bochum",
    "1. FC Heidenheim 1846": "Heidenheim",
    "Heidenheim": "Heidenheim",
    "1. FC Union Berlin": "Union Berlin",
    "FC Augsburg": "Augsburg",
    "FSV Mainz 05": "Mainz",
    "Mainz 05": "Mainz",
    "SC Freiburg": "Freiburg",
    "Werder Bremen": "Werder Bremen",
    "Hoffenheim": "Hoffenheim",
    "Holstein Kiel": "Holstein Kiel",
    "St. Pauli": "St Pauli",
    # Serie A
    "Inter": "Inter",
    "AC Milan": "Milan",
    "AS Roma": "Roma",
    "Hellas Verona": "Verona",
    "Venezia": "Venezia",
    "Monza": "Monza",
    "Lecce": "Lecce",
    "Empoli": "Empoli",
    "Parma Calcio 1913": "Parma",
    "Parma": "Parma",
    "Cremonese": "Cremonese",
    # Ligue 1
    "Paris Saint Germain": "Paris SG",
    "Paris Saint-Germain": "Paris SG",
    "Olympique Marseille": "Marseille",
    "Olympique Lyonnais": "Lyon",
    "AS Monaco": "Monaco",
    "Stade Rennes": "Rennes",
    "Stade Reims": "Reims",
    "Stade Brestois 29": "Brest",
    "Brest": "Brest",
    "Montpellier": "Montpellier",
    "Nantes": "Nantes",
    "Nice": "Nice",
    "Lille": "Lille",
    "Toulouse": "Toulouse",
    "RC Lens": "Lens",
    "Lens": "Lens",
    "Strasbourg": "Strasbourg",
    "Auxerre": "Auxerre",
    "Angers": "Angers",
    "Le Havre": "Le Havre",
    "Saint-Etienne": "St Etienne",
    "Saint Etienne": "St Etienne",
    "Paris FC": "Paris FC",
    "Metz": "Metz",
}


_COMMON_WORDS = {"fc", "cf", "ac", "as", "rc", "sc", "ssc", "cd", "ud", "sd", "afc", "club", "the"}


def _normalize(name: str) -> str:
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    tokens = [t for t in s.split() if t and t not in _COMMON_WORDS]
    return " ".join(tokens)


@dataclass
class UnderstatMatch:
    league: str             # internal code, e.g. E0
    season: str             # e.g. "2025" (Understat season start year)
    date: datetime          # UTC
    home_name: str          # Understat team name (raw)
    away_name: str
    xg_home: float
    xg_away: float
    ft_home: int | None
    ft_away: int | None


def fetch_season(understat_league: str, season: int) -> list[UnderstatMatch]:
    """Fetch every match of `season` on Understat for one league.

    Season 2024 = "2024/25" on the site.
    """
    url = f"https://understat.com/league/{understat_league}/{season}"
    internal_league = LEAGUE_MAP[understat_league]

    resp = httpx.get(
        url,
        timeout=30.0,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    resp.raise_for_status()
    html = resp.text

    # Understat embeds match-level data in `var datesData = JSON.parse('...')`.
    # The string is \x-escaped; we pull it out and let Python decode the escapes.
    m = re.search(r"var\s+datesData\s*=\s*JSON\.parse\('([^']+)'\)", html)
    if not m:
        raise RuntimeError(
            f"No datesData on {url} — are we Turkey-blocked? "
            f"Page size: {len(html)} bytes (real pages are >100KB)."
        )
    raw = m.group(1)
    decoded = bytes(raw, "utf-8").decode("unicode_escape")
    matches = json.loads(decoded)

    out: list[UnderstatMatch] = []
    for row in matches:
        # row fields: id, isResult, h{id,title,short_title}, a{...},
        #             goals{h,a}, xG{h,a}, datetime, forecast{w,d,l}
        try:
            is_result = bool(row.get("isResult", False))
            dt_str = row.get("datetime")  # "2024-08-17 14:00:00"
            dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
            home_name = row["h"]["title"]
            away_name = row["a"]["title"]
            xg_h = float(row["xG"]["h"]) if row["xG"]["h"] is not None else None
            xg_a = float(row["xG"]["a"]) if row["xG"]["a"] is not None else None
            if xg_h is None or xg_a is None:
                continue  # scheduled future match, no xG yet
            ft_h = int(row["goals"]["h"]) if is_result and row["goals"]["h"] is not None else None
            ft_a = int(row["goals"]["a"]) if is_result and row["goals"]["a"] is not None else None
        except (KeyError, ValueError, TypeError):
            continue

        out.append(
            UnderstatMatch(
                league=internal_league,
                season=str(season),
                date=dt,
                home_name=home_name,
                away_name=away_name,
                xg_home=xg_h,
                xg_away=xg_a,
                ft_home=ft_h,
                ft_away=ft_a,
            )
        )
    return out


def _build_team_index(db: Session, league: str) -> dict[str, Team]:
    """name (normalized) → Team row, for one league."""
    teams = db.scalars(select(Team).where(Team.league == league)).all()
    return {_normalize(t.name): t for t in teams}


def _resolve_team(
    understat_name: str,
    normalized_index: dict[str, Team],
) -> Team | None:
    """Map an Understat team name to a Team row via alias table + normalization."""
    # Try alias first
    aliased = TEAM_ALIASES.get(understat_name)
    if aliased:
        norm = _normalize(aliased)
        if norm in normalized_index:
            return normalized_index[norm]
    # Direct normalize
    norm = _normalize(understat_name)
    return normalized_index.get(norm)


def apply_xg(db: Session, rows: list[UnderstatMatch]) -> tuple[int, int, set[str]]:
    """Write xg_home / xg_away onto existing matches in the DB.

    We match by (league, both teams, kickoff date) rather than exact kickoff
    datetime, because the hour sometimes drifts between sources.

    Returns (updated, skipped_team_match, skipped_no_db_match, unmatched_names).
    """
    updated = 0
    skipped = 0
    unmatched: set[str] = set()

    # Group rows by league to avoid rebuilding the team index repeatedly.
    by_league: dict[str, list[UnderstatMatch]] = {}
    for r in rows:
        by_league.setdefault(r.league, []).append(r)

    for league, league_rows in by_league.items():
        idx = _build_team_index(db, league)

        for r in league_rows:
            home = _resolve_team(r.home_name, idx)
            away = _resolve_team(r.away_name, idx)
            if home is None:
                unmatched.add(r.home_name)
            if away is None:
                unmatched.add(r.away_name)
            if home is None or away is None:
                skipped += 1
                continue

            # Match on same calendar date — kickoff hours can differ by source.
            day_start = r.date.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = day_start.replace(hour=23, minute=59, second=59)

            m = db.scalar(
                select(Match).where(
                    Match.league == league,
                    Match.home_team_id == home.id,
                    Match.away_team_id == away.id,
                    Match.kickoff >= day_start,
                    Match.kickoff <= day_end,
                )
            )
            if m is None:
                skipped += 1
                continue

            m.xg_home = r.xg_home
            m.xg_away = r.xg_away
            updated += 1

    db.commit()
    return updated, skipped, unmatched


def run(seasons: list[int], leagues: list[str] | None = None, sleep_s: float = 1.0) -> None:
    targets = leagues if leagues else list(LEAGUE_MAP.keys())
    with SessionLocal() as db:
        for league_slug in targets:
            for season in seasons:
                print(f"Fetching {league_slug} {season}/{season + 1}...", flush=True)
                try:
                    rows = fetch_season(league_slug, season)
                except Exception as e:
                    print(f"  failed: {e}", flush=True)
                    continue
                print(f"  got {len(rows)} matches with xG", flush=True)
                if not rows:
                    continue
                updated, skipped, unmatched = apply_xg(db, rows)
                print(
                    f"  applied xG to {updated} DB matches "
                    f"({skipped} skipped — unmatched teams or no fixture row)",
                    flush=True,
                )
                if unmatched:
                    print(f"  unmatched team names (add to TEAM_ALIASES):", flush=True)
                    for n in sorted(unmatched):
                        print(f"    {n!r}", flush=True)
                time.sleep(sleep_s)


def main() -> None:
    p = argparse.ArgumentParser(description="Understat xG ingester")
    p.add_argument(
        "--seasons",
        default="2023,2024,2025",
        help="Comma-separated Understat season start years (e.g. 2024 = 2024/25).",
    )
    p.add_argument(
        "--leagues",
        default=None,
        help=f"Comma-separated Understat league slugs. Default: all ({','.join(LEAGUE_MAP)}).",
    )
    args = p.parse_args()

    seasons = [int(s) for s in args.seasons.split(",")]
    leagues = args.leagues.split(",") if args.leagues else None
    run(seasons=seasons, leagues=leagues)


if __name__ == "__main__":
    main()
