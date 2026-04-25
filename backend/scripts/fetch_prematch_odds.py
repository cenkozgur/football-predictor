"""Pre-match odds fetcher for upcoming fixtures (api-football).

Why this exists
---------------
football-data.co.uk's CSV — our historical odds source — only updates after
matches finish, so the day-of CSV has yesterday's odds, not today's
upcoming. The composer's value-edge gate needs market prices for *future*
matches to compute edge, and without them every league_policy filter
suppresses the pick.

api-football's /odds endpoint serves the same fixtures with fresh
bookmaker prices, including pre-match. We map the api-football fixture
id back to our Match row by (date, league, teams) and write the result
into our existing Odds table using a synthetic source name.

Idempotent
----------
We only insert odds rows that don't already exist. So a re-run during the
day refreshes nothing, and a daily run just fills in newly-ingested
fixtures without duplicating yesterday's data.

Dry-run contract
----------------
If FOOTBALL_API_KEY isn't set, exit 0 with a warning — the workflow can
call us regardless.

Usage
-----
    python -m scripts.fetch_prematch_odds --days-ahead 3
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.orm import joinedload

from app.db import SessionLocal
from app.ingestion.api_football import (
    LEAGUE_MAP,
    _build_team_index,
    _client as af_client_factory,
    _get,
    _resolve_team,
)
from app.models.match import Match
from app.models.odds import Odds
from app.models.team import Team


# Source label written to Odds.source so we can tell apart api-football
# pre-match odds from football-data.co.uk closing odds. The composer's
# _SOURCE_PRIORITY treats unknown labels as priority 99 (lowest), which
# is correct — closing odds are still preferred when present.
_SOURCE_LABEL = "AFP"  # api-football pre-match


# api-football market name → our internal market label
_MARKET_NAME_MAP = {
    "Match Winner": "1X2",
    "Both Teams Score": "btts",
    "Double Chance": "double_chance",
    "Goals Over/Under": None,  # handled specially per line
}

# Selection-name maps per market
_SEL_1X2 = {"Home": "1", "Draw": "X", "Away": "2"}
_SEL_BTTS = {"Yes": "yes", "No": "no"}
_SEL_DC = {"Home/Draw": "1X", "Home/Away": "12", "Draw/Away": "X2"}


def _upcoming_matches(db, days_ahead: int) -> list[Match]:
    now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    end = now + timedelta(days=days_ahead)
    stmt = (
        select(Match)
        .options(joinedload(Match.home_team), joinedload(Match.away_team))
        .where(Match.status == "scheduled")
        .where(Match.kickoff > now)
        .where(Match.kickoff <= end)
        .where(Match.league.in_(LEAGUE_MAP.keys()))
        .order_by(Match.kickoff)
    )
    return list(db.scalars(stmt).all())


def _has_odds(db, match_id: int) -> bool:
    """True if any odds row already exists for this match."""
    return db.query(Odds).filter(Odds.match_id == match_id).first() is not None


def _find_fixture_id(
    client, league_id: int, season: int, date_iso: str,
    home_name: str, away_name: str,
    team_index: dict[str, Team], fuzzy: dict[str, Team | None],
) -> int | None:
    """Find api-football fixture id for our match on the given date."""
    try:
        data = _get(
            client,
            "/fixtures",
            {"league": league_id, "season": season, "date": date_iso},
        )
    except Exception as exc:
        print(f"  fixtures fetch failed {league_id}/{date_iso}: {exc}")
        return None

    for row in data.get("response", []):
        h = (row.get("teams") or {}).get("home") or {}
        a = (row.get("teams") or {}).get("away") or {}
        rh = _resolve_team(h.get("name", ""), team_index, fuzzy)
        ra = _resolve_team(a.get("name", ""), team_index, fuzzy)
        if rh and ra and rh.name == home_name and ra.name == away_name:
            return (row.get("fixture") or {}).get("id")
    return None


def _odds_rows_from_response(match_id: int, response: list[dict[str, Any]]) -> list[Odds]:
    """Parse api-football /odds response into our Odds rows.

    api-football returns a list of bookmakers per fixture, each with bets
    (markets) and values (selections). We pick the first bookmaker (typically
    Bet365 or Pinnacle on this provider) and flatten.
    """
    if not response:
        return []
    bookmakers = response[0].get("bookmakers") or []
    if not bookmakers:
        return []
    book = bookmakers[0]
    out: list[Odds] = []

    for bet in book.get("bets") or []:
        name = bet.get("name")
        for v in bet.get("values") or []:
            sel_raw = v.get("value")
            try:
                odd = float(v.get("odd"))
            except (TypeError, ValueError):
                continue
            if odd <= 1.01:
                continue

            if name == "Match Winner":
                sel = _SEL_1X2.get(sel_raw)
                if sel:
                    out.append(Odds(
                        match_id=match_id, source=_SOURCE_LABEL,
                        market="1X2", selection=sel, decimal_odds=odd,
                    ))
            elif name == "Both Teams Score":
                sel = _SEL_BTTS.get(sel_raw)
                if sel:
                    out.append(Odds(
                        match_id=match_id, source=_SOURCE_LABEL,
                        market="btts", selection=sel, decimal_odds=odd,
                    ))
            elif name == "Double Chance":
                sel = _SEL_DC.get(sel_raw)
                if sel:
                    out.append(Odds(
                        match_id=match_id, source=_SOURCE_LABEL,
                        market="double_chance", selection=sel, decimal_odds=odd,
                    ))
            elif name == "Goals Over/Under":
                # sel_raw like "Over 2.5" / "Under 2.5"
                parts = (sel_raw or "").split()
                if len(parts) != 2:
                    continue
                side, line = parts
                if side not in ("Over", "Under"):
                    continue
                out.append(Odds(
                    match_id=match_id, source=_SOURCE_LABEL,
                    market=f"OU_{line}",
                    selection=side.lower(),
                    decimal_odds=odd,
                ))
    return out


def run(days_ahead: int = 3) -> None:
    api_key = os.environ.get("FOOTBALL_API_KEY")
    if not api_key:
        print("FOOTBALL_API_KEY not set — skipping pre-match odds fetch.")
        sys.exit(0)

    written_matches = 0
    written_rows = 0
    skipped_already_have = 0
    not_found = 0
    api_calls = 0

    with af_client_factory(api_key) as client, SessionLocal() as db:
        matches = _upcoming_matches(db, days_ahead)
        if not matches:
            print("No upcoming matches in window — nothing to fetch.")
            return

        team_index = _build_team_index(db)
        fuzzy: dict[str, Team | None] = {}

        print(f"Fetching pre-match odds for {len(matches)} upcoming fixtures…")

        # Cache fixture id lookups per (league, date) so multiple matches
        # the same day share one /fixtures call.
        fixtures_by_day: dict[tuple[str, str], dict[tuple[str, str], int]] = {}

        def _fixtures_lookup(league_code: str, date_iso: str) -> dict[tuple[str, str], int]:
            key = (league_code, date_iso)
            if key in fixtures_by_day:
                return fixtures_by_day[key]
            api_id, season = LEAGUE_MAP[league_code]
            try:
                data = _get(
                    client,
                    "/fixtures",
                    {"league": api_id, "season": season, "date": date_iso},
                )
            except Exception as exc:
                print(f"  fixtures fetch failed {league_code}/{date_iso}: {exc}")
                fixtures_by_day[key] = {}
                return {}
            nonlocal api_calls
            api_calls += 1
            mapping: dict[tuple[str, str], int] = {}
            for row in data.get("response", []):
                h = (row.get("teams") or {}).get("home") or {}
                a = (row.get("teams") or {}).get("away") or {}
                rh = _resolve_team(h.get("name", ""), team_index, fuzzy)
                ra = _resolve_team(a.get("name", ""), team_index, fuzzy)
                if rh and ra:
                    fid = (row.get("fixture") or {}).get("id")
                    if fid:
                        mapping[(rh.name, ra.name)] = fid
            fixtures_by_day[key] = mapping
            return mapping

        for m in matches:
            if _has_odds(db, m.id):
                skipped_already_have += 1
                continue
            if m.league not in LEAGUE_MAP:
                continue
            date_iso = m.kickoff.date().isoformat()
            fixtures_map = _fixtures_lookup(m.league, date_iso)
            fixture_id = fixtures_map.get((m.home_team.name, m.away_team.name))
            if fixture_id is None:
                not_found += 1
                continue

            try:
                odds_data = _get(client, "/odds", {"fixture": fixture_id})
                api_calls += 1
            except Exception as exc:
                print(f"  odds fetch failed for fixture {fixture_id}: {exc}")
                continue

            new_rows = _odds_rows_from_response(m.id, odds_data.get("response") or [])
            if not new_rows:
                continue

            for r in new_rows:
                db.add(r)
            written_matches += 1
            written_rows += len(new_rows)

        db.commit()

    print(
        f"Pre-match odds: wrote {written_rows} rows across {written_matches} matches; "
        f"{skipped_already_have} already had odds, {not_found} unmatched fixtures, "
        f"{api_calls} api-football calls."
    )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--days-ahead", type=int, default=3)
    args = p.parse_args()
    run(days_ahead=args.days_ahead)


if __name__ == "__main__":
    main()
