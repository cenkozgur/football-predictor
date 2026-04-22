"""Backfill xg_home / xg_away from api-football /fixtures/statistics.

Replacement for `app.ingestion.understat`, which is blocked by Understat's
anti-bot layer from every cloud IP we can reach. api-football's statistics
endpoint exposes Expected Goals per fixture (for leagues where the provider
computes it — essentially the same top-5 + a handful of others).

Strategy
--------
1. Find every finished match in the window that still has NULL xg_home.
2. For each, look up the api-football fixture id (we don't store it) by
   querying /fixtures with (league, season, date) and matching teams.
3. Pull /fixtures/statistics?fixture=<id>, extract the "expected_goals"
   type from each team's stats array, write to our Match row.

Rate-limit posture
------------------
Pro plan: 7,500 req/day. A 7-day backfill across 5 leagues is usually
30-70 finished matches per run, so ~140 calls (one /fixtures call per
league-day + one /fixtures/statistics per match). Well inside the daily
budget, but we still cache aggressively via ApiFootballClient so a rerun
on the same day is nearly free.

Dry-run contract
----------------
If FOOTBALL_API_KEY isn't set we exit 0 with a warning — the workflow can
call us unconditionally before the key is wired up.

Usage
-----
    export FOOTBALL_API_KEY=...
    python -m scripts.fetch_xg_api_football --days-back 7
"""

from __future__ import annotations

import argparse
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import joinedload

from app.db import SessionLocal
from app.ingestion.api_football import (
    LEAGUE_MAP,
    _build_team_index,
    _resolve_team,
)
from app.models.match import Match
from app.models.team import Team


# Only leagues where api-football reports Expected Goals. These are the
# same leagues Understat covered, plus a few extras the provider now has
# xG feeds for (Eredivisie, Primeira Liga). Other leagues still fall
# through to goals-only DC, same behavior as before.
_XG_LEAGUES = {"E0", "D1", "I1", "SP1", "F1", "N1", "P1"}


def _new_client():
    """Lazy import so --help works without the key set."""
    from app.ingestion.api_football import _client

    key = os.environ.get("FOOTBALL_API_KEY")
    if not key:
        return None
    return _client(key)


def _finished_matches_without_xg(db, days_back: int) -> list[Match]:
    since = datetime.now(tz=timezone.utc).replace(tzinfo=None) - timedelta(days=days_back)
    stmt = (
        select(Match)
        .options(joinedload(Match.home_team), joinedload(Match.away_team))
        .where(Match.league.in_(_XG_LEAGUES))
        .where(Match.status == "finished")
        .where(Match.kickoff >= since)
        .where(or_(Match.xg_home.is_(None), Match.xg_away.is_(None)))
        .order_by(Match.kickoff.desc())
    )
    return list(db.scalars(stmt).all())


def _list_fixtures_for_day(client, league_id: int, season: int, date_iso: str) -> list[dict[str, Any]]:
    """One /fixtures call returning every fixture for that league on that date.

    We query by date (UTC) rather than by fixture id because we don't store
    api-football's id. The response typically has 1-5 fixtures per
    league-day, so looking up the right one is a small O(N) scan.
    """
    from app.ingestion.api_football import _get

    data = _get(
        client,
        "/fixtures",
        {
            "league": league_id,
            "season": season,
            "date": date_iso,
        },
    )
    return data.get("response", [])


def _match_fixture_id(
    api_rows: list[dict[str, Any]],
    home_name: str,
    away_name: str,
    team_index: dict[str, Team],
    fuzzy_cache: dict[str, Team | None],
) -> int | None:
    """Find the api-football fixture.id for one of our matches.

    The team-name spelling differs across providers, so we resolve both sides
    through the same alias/fuzzy path the fixture ingester uses.
    """
    for row in api_rows:
        home_row = (row.get("teams") or {}).get("home") or {}
        away_row = (row.get("teams") or {}).get("away") or {}
        resolved_home = _resolve_team(home_row.get("name", ""), team_index, fuzzy_cache)
        resolved_away = _resolve_team(away_row.get("name", ""), team_index, fuzzy_cache)
        if resolved_home is None or resolved_away is None:
            continue
        if resolved_home.name == home_name and resolved_away.name == away_name:
            return (row.get("fixture") or {}).get("id")
    return None


def _extract_xg(stats_response: dict[str, Any]) -> tuple[float | None, float | None]:
    """Pull (home_xg, away_xg) from /fixtures/statistics output.

    Response shape: `response` is a list of {team: {id, name}, statistics:
    [{type, value}, ...]}. The xG entry's `type` is "expected_goals" (or
    "Expected Goals" — casing varies). Value is a string like "1.54" or
    null when the provider doesn't compute it for this match.
    """
    response = stats_response.get("response") or []
    if len(response) < 2:
        return None, None

    def _xg_from(stats_list: list[dict[str, Any]]) -> float | None:
        for item in stats_list:
            type_name = (item.get("type") or "").strip().lower()
            if type_name == "expected_goals" or type_name == "expected goals":
                val = item.get("value")
                if val is None:
                    return None
                try:
                    return float(val)
                except (TypeError, ValueError):
                    return None
        return None

    home_entry = response[0]
    away_entry = response[1]
    home_xg = _xg_from(home_entry.get("statistics") or [])
    away_xg = _xg_from(away_entry.get("statistics") or [])
    return home_xg, away_xg


def run(days_back: int = 7) -> None:
    client = _new_client()
    if client is None:
        print("FOOTBALL_API_KEY not set — skipping xG backfill (dry-run).")
        return

    from app.ingestion.api_football import _get

    written = 0
    no_xg = 0
    unmatched_fixture = 0
    api_calls = 0

    with client, SessionLocal() as db:
        matches = _finished_matches_without_xg(db, days_back=days_back)
        if not matches:
            print("No finished matches missing xG in window — nothing to do.")
            return

        print(
            f"{len(matches)} finished matches missing xG in last "
            f"{days_back} days. Beginning backfill…"
        )

        team_index = _build_team_index(db)
        fuzzy_cache: dict[str, Team | None] = {}

        # Group by (league, date) so one /fixtures call covers every match
        # played on that day in that league — cheaper than per-match lookups.
        by_league_day: dict[tuple[str, str], list[Match]] = defaultdict(list)
        for m in matches:
            date_iso = m.kickoff.date().isoformat()
            by_league_day[(m.league, date_iso)].append(m)

        for (league_code, date_iso), day_matches in by_league_day.items():
            if league_code not in LEAGUE_MAP:
                continue
            api_id, season = LEAGUE_MAP[league_code]
            try:
                day_fixtures = _list_fixtures_for_day(
                    client, api_id, season, date_iso
                )
                api_calls += 1
            except Exception as exc:
                print(f"  [{league_code} {date_iso}] /fixtures failed: {exc}")
                continue

            for m in day_matches:
                home_name = m.home_team.name
                away_name = m.away_team.name
                fixture_id = _match_fixture_id(
                    day_fixtures, home_name, away_name, team_index, fuzzy_cache
                )
                if fixture_id is None:
                    unmatched_fixture += 1
                    continue

                try:
                    stats_data = _get(
                        client,
                        "/fixtures/statistics",
                        {"fixture": fixture_id},
                    )
                    api_calls += 1
                except Exception as exc:
                    print(f"  fixture {fixture_id} stats failed: {exc}")
                    continue

                home_xg, away_xg = _extract_xg(stats_data)
                if home_xg is None or away_xg is None:
                    no_xg += 1
                    continue

                m.xg_home = home_xg
                m.xg_away = away_xg
                written += 1

        db.commit()

    print(
        f"xG backfill: wrote {written} matches, "
        f"{no_xg} had no xG reported, "
        f"{unmatched_fixture} unmatched fixture ids, "
        f"{api_calls} api-football calls."
    )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--days-back", type=int, default=7)
    args = p.parse_args()
    run(days_back=args.days_back)


if __name__ == "__main__":
    main()
