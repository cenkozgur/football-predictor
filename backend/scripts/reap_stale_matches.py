"""Reconcile matches that stayed 'scheduled' long after their kickoff.

Problem this fixes
------------------
`fetch_live_scores` only promotes a match to 'finished' when it happens to
observe it in-play or just-finished during its 10-23 UTC polling window.
Matches in leagues we ingest fixtures for but *don't* actively poll during
their match window (FIN, IRL, NOR etc. — Nordic summer leagues with evening
kickoffs often outside our window) get stuck as 'scheduled' with a past
kickoff. The UI's "Yaklaşan" list then shows 12-day-old fixtures as if
they were upcoming.

This script sweeps the DB for matches whose kickoff is > 12h ago but
status is still 'scheduled', looks each up via football-data.org's historic
endpoint, and settles ft_home/ft_away + flips status. Safe to re-run: it
only touches rows whose ft_home is still NULL.

Usage
-----
    export FOOTBALL_DATA_ORG_KEY=...
    python -m scripts.reap_stale_matches --days-back 30
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.db import SessionLocal
from app.ingestion.football_data_org import LEAGUE_MAP as FDO_LEAGUE_MAP, TEAM_ALIASES
from difflib import get_close_matches

from app.ingestion.api_football import (
    LEAGUE_MAP as AF_LEAGUE_MAP,
    TEAM_ALIASES as AF_TEAM_ALIASES,
    _build_team_index,
    _normalize as _af_normalize,
)
from app.models.match import Match
from app.models.team import Team


def _loose_resolve_team(
    name: str,
    team_index: dict[str, Team],
    fuzzy_cache: dict[str, Team | None],
    cutoff: float = 0.72,
) -> Team | None:
    """Looser variant of api_football._resolve_team for the reaper's use.

    The daily fixture ingester uses 0.88 cutoff because a bad match there
    writes a wrong row permanently. The reaper only *updates* status +
    scores on rows we already have; a misassigned settlement is still
    bounded to one match's score, not a new inserted team. So we widen
    the net so FIN/IRL/NOR spellings that differ cosmetically from our DB
    can still settle.
    """
    if name in AF_TEAM_ALIASES:
        norm = _af_normalize(AF_TEAM_ALIASES[name])
        if norm in team_index:
            return team_index[norm]
    norm = _af_normalize(name)
    if norm in team_index:
        return team_index[norm]
    if name in fuzzy_cache:
        return fuzzy_cache[name]
    keys = list(team_index.keys())
    close = get_close_matches(norm, keys, n=1, cutoff=cutoff)
    team = team_index[close[0]] if close else None
    fuzzy_cache[name] = team
    return team


API_BASE = "https://api.football-data.org/v4"


def _client(api_key: str) -> httpx.Client:
    return httpx.Client(
        base_url=API_BASE,
        headers={"X-Auth-Token": api_key},
        timeout=30.0,
    )


def _stale_matches(db, days_back: int) -> list[Match]:
    """`scheduled` rows whose kickoff is >12h in the past, within window."""
    now_naive = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    cutoff_recent = now_naive - timedelta(hours=12)
    cutoff_old = now_naive - timedelta(days=days_back)
    stmt = (
        select(Match)
        .options(joinedload(Match.home_team), joinedload(Match.away_team))
        .where(Match.status == "scheduled")
        .where(Match.kickoff < cutoff_recent)
        .where(Match.kickoff >= cutoff_old)
        .order_by(Match.kickoff.asc())
    )
    return list(db.scalars(stmt).all())


def _fetch_finished_for_league(
    client: httpx.Client,
    fdo_code: str,
    date_from: str,
    date_to: str,
) -> list[dict[str, Any]]:
    """One call returns every FINISHED fixture for that league in the window."""
    r = client.get(
        f"/competitions/{fdo_code}/matches",
        params={"dateFrom": date_from, "dateTo": date_to, "status": "FINISHED"},
    )
    r.raise_for_status()
    return r.json().get("matches", [])


def _names_match(api_name: str, db_name: str) -> bool:
    """Match two team spellings leniently — alias-resolve + normalized compare.

    Tighter than the api-football fuzzy path because here we already have the
    DB-side team name to compare against directly; we don't need to scan the
    whole team index. Handles cases like 'Manchester United FC' vs
    'Man United' and 'Borussia Dortmund' vs 'Dortmund'.
    """
    if api_name == db_name:
        return True
    aliased = TEAM_ALIASES.get(api_name, api_name)
    if aliased == db_name:
        return True
    # Last resort: normalized + substring either way so 'Dortmund' ~ 'BVB
    # Dortmund' still matches. Cheap and bounded in blast radius.
    n_api = _af_normalize(aliased)
    n_db = _af_normalize(db_name)
    if not n_api or not n_db:
        return False
    return n_api == n_db or n_api in n_db or n_db in n_api


def _find_provider_match(
    provider_rows: list[dict[str, Any]], home_name: str, away_name: str, kickoff: datetime
) -> dict[str, Any] | None:
    """Match our DB row to a provider fixture via (teams, kickoff ±4h)."""
    for row in provider_rows:
        api_home = (row.get("homeTeam") or {}).get("name")
        api_away = (row.get("awayTeam") or {}).get("name")
        if not (api_home and api_away):
            continue
        if not (_names_match(api_home, home_name) and _names_match(api_away, away_name)):
            continue
        utc_str = row.get("utcDate")
        try:
            api_kickoff = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            continue
        api_kickoff_naive = api_kickoff.astimezone(timezone.utc).replace(tzinfo=None)
        if abs((api_kickoff_naive - kickoff).total_seconds()) < 4 * 3600:
            return row
    return None


def _reap_via_api_football(
    db,
    league_code: str,
    matches: list[Match],
    team_index: dict[str, Team],
    fuzzy_cache: dict[str, Team | None],
) -> tuple[int, int]:
    """Settle stale matches for a league that's not on football-data.org.

    Uses api-football /fixtures?league=X&season=Y&date=D per match date, which
    is cheaper than /fixtures/status=FINISHED (that endpoint doesn't accept a
    date range narrower than a season). Returns (resolved, unresolved).
    """
    from app.ingestion.api_football import _client as af_client_factory, _get

    api_key = os.environ.get("FOOTBALL_API_KEY")
    if not api_key:
        # Without the key we can't call api-football — leave these alone so
        # we don't corrupt status silently.
        return 0, len(matches)

    api_id, season = AF_LEAGUE_MAP[league_code]
    resolved = 0
    unresolved = 0
    by_date: dict[str, list[Match]] = {}
    for m in matches:
        by_date.setdefault(m.kickoff.date().isoformat(), []).append(m)

    with af_client_factory(api_key) as client:
        for date_iso, day_matches in by_date.items():
            try:
                data = _get(
                    client,
                    "/fixtures",
                    {"league": api_id, "season": season, "date": date_iso},
                )
            except Exception as exc:
                print(f"  [{league_code} {date_iso}] api-football failed: {exc}")
                unresolved += len(day_matches)
                continue

            rows = data.get("response", [])
            for m in day_matches:
                matched_row = None
                for row in rows:
                    home_row = (row.get("teams") or {}).get("home") or {}
                    away_row = (row.get("teams") or {}).get("away") or {}
                    resolved_home = _loose_resolve_team(
                        home_row.get("name", ""), team_index, fuzzy_cache
                    )
                    resolved_away = _loose_resolve_team(
                        away_row.get("name", ""), team_index, fuzzy_cache
                    )
                    if resolved_home is None or resolved_away is None:
                        continue
                    if (
                        resolved_home.id == m.home_team_id
                        and resolved_away.id == m.away_team_id
                    ):
                        matched_row = row
                        break

                if matched_row is None:
                    unresolved += 1
                    continue
                status = ((matched_row.get("fixture") or {}).get("status") or {}).get("short")
                if status not in ("FT", "AET", "PEN"):
                    unresolved += 1
                    continue
                goals = matched_row.get("goals") or {}
                home_goals = goals.get("home")
                away_goals = goals.get("away")
                if home_goals is None or away_goals is None:
                    unresolved += 1
                    continue
                m.ft_home = int(home_goals)
                m.ft_away = int(away_goals)
                m.status = "finished"
                resolved += 1

    return resolved, unresolved


def run(days_back: int = 30) -> None:
    api_key = os.environ.get("FOOTBALL_DATA_ORG_KEY")
    if not api_key:
        print("FOOTBALL_DATA_ORG_KEY not set — skipping stale-match reap.")
        sys.exit(0)

    resolved = 0
    unresolved = 0
    leagues_skipped = 0

    with _client(api_key) as client, SessionLocal() as db:
        stale = _stale_matches(db, days_back=days_back)
        if not stale:
            print("No stale scheduled matches — nothing to do.")
            return

        print(f"Found {len(stale)} stale scheduled matches; reconciling…")

        # Group by (league, date window) so one provider call per league covers
        # many stale rows. We widen to the full requested range since the
        # provider's response is cheap and we already bucket results locally.
        by_league: dict[str, list[Match]] = {}
        for m in stale:
            by_league.setdefault(m.league, []).append(m)

        date_from = (
            datetime.now(tz=timezone.utc).date() - timedelta(days=days_back)
        ).isoformat()
        date_to = datetime.now(tz=timezone.utc).date().isoformat()

        af_team_index = _build_team_index(db)
        af_fuzzy_cache: dict[str, Team | None] = {}

        for league_code, matches in by_league.items():
            if league_code in FDO_LEAGUE_MAP:
                fdo_code = FDO_LEAGUE_MAP[league_code]
                try:
                    provider_rows = _fetch_finished_for_league(
                        client, fdo_code, date_from, date_to
                    )
                except httpx.HTTPError as exc:
                    print(f"  [{league_code}] provider fetch failed: {exc}")
                    continue

                for m in matches:
                    row = _find_provider_match(
                        provider_rows, m.home_team.name, m.away_team.name, m.kickoff
                    )
                    if row is None:
                        unresolved += 1
                        continue
                    full = (row.get("score") or {}).get("fullTime") or {}
                    home_goals = full.get("home")
                    away_goals = full.get("away")
                    if home_goals is None or away_goals is None:
                        unresolved += 1
                        continue
                    m.ft_home = int(home_goals)
                    m.ft_away = int(away_goals)
                    m.status = "finished"
                    resolved += 1
            elif league_code in AF_LEAGUE_MAP:
                # Fallback to api-football for leagues outside FDO free tier
                # (FIN, IRL, NOR, POL, SC0, T1, …). We already have the key
                # in env for availability + xG fetchers.
                resolved_here, unresolved_here = _reap_via_api_football(
                    db, league_code, matches, af_team_index, af_fuzzy_cache
                )
                resolved += resolved_here
                unresolved += unresolved_here
            else:
                leagues_skipped += 1

        db.commit()

    print(
        f"Reaper: resolved {resolved}, unresolved {unresolved}, "
        f"leagues outside provider {leagues_skipped}."
    )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--days-back", type=int, default=30)
    args = p.parse_args()
    run(days_back=args.days_back)


if __name__ == "__main__":
    main()
