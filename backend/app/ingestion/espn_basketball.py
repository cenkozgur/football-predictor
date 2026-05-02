"""ESPN public NBA scoreboard ingester for SportEvent.

ESPN exposes an undocumented public scoreboard endpoint that returns
games for a given calendar day. No API key needed, no rate limit
documented (we keep our calls modest anyway). Drops into sport_events
as sport='basketball', league='NBA'.

EuroLeague + BSL are NOT covered by ESPN's basketball scoreboard
(probed 2026-04-26: HTTP 400 for both). Those leagues remain
static-only on the Bugün Ne Var side until we either add scraping or
upgrade to api-sports paid.

Usage:
    python -m app.ingestion.espn_basketball --days-ahead 14
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import select

from app.db import SessionLocal
from app.models.sport_event import SportEvent


SCOREBOARD_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
)


def _client() -> httpx.Client:
    # ESPN's public API doesn't require auth, but a UA helps us avoid
    # the occasional bot block their CDN does.
    return httpx.Client(
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; bugun-ne-var/1.0; "
                "+https://bugun-ne-var.base44.app)"
            ),
            "Accept": "application/json",
        },
        timeout=30.0,
    )


def fetch_day(client: httpx.Client, date_str: str) -> list[dict[str, Any]]:
    """date_str: YYYYMMDD"""
    r = client.get(SCOREBOARD_URL, params={"dates": date_str})
    r.raise_for_status()
    return r.json().get("events", []) or []


# ESPN status.type.state values: 'pre' = scheduled, 'in' = live,
# 'post' = finished. type.completed is the authoritative finished flag.
def normalize_status(status_obj: dict[str, Any] | None) -> str:
    if not status_obj:
        return "scheduled"
    t = status_obj.get("type") or {}
    if t.get("completed"):
        return "finished"
    state = t.get("state")
    if state == "in":
        return "in_play"
    if state == "post":
        return "finished"
    return "scheduled"


def parse_event(g: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    gid = g.get("id")
    date_iso = g.get("date")
    if not gid or not date_iso:
        return None
    try:
        kickoff = datetime.fromisoformat(date_iso.replace("Z", "+00:00"))
    except ValueError:
        return None

    comps = g.get("competitions") or []
    if not comps:
        return None
    comp = comps[0]
    competitors = comp.get("competitors") or []
    if len(competitors) < 2:
        return None

    # ESPN orders competitors as home first then away by `homeAway`.
    home_name = None
    away_name = None
    for c in competitors:
        team = (c.get("team") or {}).get("displayName")
        if not team:
            continue
        if c.get("homeAway") == "home":
            home_name = team
        elif c.get("homeAway") == "away":
            away_name = team
    # Fallback if homeAway absent: use order
    if not home_name and len(competitors) >= 1:
        home_name = (competitors[0].get("team") or {}).get("displayName")
    if not away_name and len(competitors) >= 2:
        away_name = (competitors[1].get("team") or {}).get("displayName")
    if not home_name or not away_name:
        return None
    # ESPN populates the bracket with "TBD vs TBD" placeholders for
    # later-round matchups whose participants aren't decided yet.
    # These have stable ext_refs so they upsert cleanly, but they are
    # noise for end users — Bugün Ne Var would show "TBD vs TBD" cards
    # in the Yakında list. Drop them at ingest time; once ESPN fills
    # the names in, our next run will pick them up properly.
    if home_name.strip().upper() == "TBD" or away_name.strip().upper() == "TBD":
        return None

    venue = ((comp.get("venue") or {}).get("fullName")) or ""
    status = normalize_status(g.get("status"))

    ext_ref = f"espn:nba:{gid}"
    fields = {
        "sport": "basketball",
        "league": "NBA",
        "season": "2025-2026",  # ESPN doesn't expose season label cleanly; hard-code current.
        "kickoff": kickoff,
        "home_team": home_name,
        "away_team": away_name,
        "venue": venue,
        "broadcaster": None,
        "status": status,
    }
    return ext_ref, fields


def upsert(session, ext_ref: str, fields: dict[str, Any]) -> str:
    existing = session.scalar(
        select(SportEvent).where(SportEvent.external_ref == ext_ref)
    )
    if existing is None:
        session.add(SportEvent(external_ref=ext_ref, **fields))
        return "inserted"
    changed = False
    for k, v in fields.items():
        if getattr(existing, k) != v:
            setattr(existing, k, v)
            changed = True
    return "updated" if changed else "skip"


def reconcile_day(session, day, espn_ext_refs: set[str]) -> int:
    """Drop ghost rows: events we previously stored as `scheduled` for
    `day` that ESPN no longer lists when we re-query that day.

    Why this exists: ESPN's scoreboard sometimes serves an
    ID for a playoff game that was reserved-but-then-not-played (e.g.
    Game 5 of a 4-2 series that ended in Game 6). We ingested it on
    day N as `scheduled`, but on day N+M ESPN drops it from the
    daily scoreboard entirely. Our row goes stale forever — Bugün
    Ne Var keeps showing the user a non-existent Lakers-Rockets game.

    Reconcile rule (intentionally narrow):
        - Same league + same kickoff calendar day
        - status == 'scheduled'   (don't touch finished/in_play games)
        - external_ref NOT in the ESPN response we just fetched
        ⇒ delete

    We never delete `finished` rows — those are historical truth and
    other parts of the app rely on them. We also never delete
    `in_play` rows mid-game (status flip happens later in upsert).
    """
    if not espn_ext_refs and day < datetime.now(tz=timezone.utc).date():
        # Empty payload for a past date is normal (ESPN trims old days).
        # Don't reconcile against an empty set or we'd wipe historical
        # rows we already marked finished. Sanity guard.
        return 0

    # Day window in UTC: ESPN's scoreboard groups by US/Eastern morning
    # but kickoffs are in UTC. A 7pm ET tipoff lands at 23:00 UTC same
    # day or 03:00 UTC next day. We err on the side of inclusion: any
    # row whose kickoff lands in the 24h centered on our query day is
    # a candidate. Slightly wide, but the external_ref check keeps it
    # from over-deleting — only rows ESPN never reported for ANY of
    # the queried days will ultimately be removed (see run() below).
    day_start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    candidates = session.scalars(
        select(SportEvent).where(
            SportEvent.sport == "basketball",
            SportEvent.league == "NBA",
            SportEvent.status == "scheduled",
            SportEvent.kickoff >= day_start,
            SportEvent.kickoff < day_end,
        )
    ).all()

    removed = 0
    for row in candidates:
        if row.external_ref in espn_ext_refs:
            continue
        session.delete(row)
        removed += 1
    return removed


def run(days_ahead: int) -> None:
    today = datetime.now(tz=timezone.utc).date()
    days = [today + timedelta(days=i) for i in range(days_ahead + 1)]
    print(f"Fetching ESPN NBA scoreboard for {len(days)} days ({days[0]} → {days[-1]})…")

    inserted = updated = unchanged = 0
    skipped = 0
    deleted = 0

    # Collect ALL refs ESPN returned across the entire window first.
    # If we reconciled per-day with only that day's refs, a game that
    # ESPN moved by ±1 day would get falsely deleted. Cross-day union
    # makes the reconcile resilient to date-bucket flips.
    espn_refs_by_day: dict[str, set[str]] = {}

    with _client() as client, SessionLocal() as session:
        for d in days:
            date_str = d.strftime("%Y%m%d")
            try:
                games = fetch_day(client, date_str)
            except httpx.HTTPStatusError as e:
                print(f"  [{date_str}] HTTP {e.response.status_code}")
                espn_refs_by_day[date_str] = set()
                continue
            except httpx.HTTPError as e:
                print(f"  [{date_str}] network error: {e}")
                espn_refs_by_day[date_str] = set()
                continue

            day_refs: set[str] = set()
            for g in games:
                parsed = parse_event(g)
                if parsed is None:
                    skipped += 1
                    continue
                ext_ref, fields = parsed
                day_refs.add(ext_ref)
                outcome = upsert(session, ext_ref, fields)
                if outcome == "inserted":
                    inserted += 1
                elif outcome == "updated":
                    updated += 1
                else:
                    unchanged += 1
            espn_refs_by_day[date_str] = day_refs

        # Reconcile pass: for each day in our window, drop any
        # `scheduled` NBA row that ESPN didn't list under ANY day of
        # this window's union. The cross-day union protects against
        # benign timezone bucket flips where ESPN lists a game one
        # calendar day off from where we stored it.
        all_espn_refs = set().union(*espn_refs_by_day.values())
        for d in days:
            if not all_espn_refs:
                # Network failure for the whole window — bail out of
                # reconcile entirely rather than mass-delete on a
                # transient outage.
                print("  reconcile skipped: ESPN returned 0 refs across window (likely network issue)")
                break
            removed = reconcile_day(session, d, all_espn_refs)
            if removed:
                print(f"  [{d.strftime('%Y%m%d')}] reconciled {removed} ghost row(s)")
            deleted += removed

        session.commit()

    print(
        f"Done. inserted={inserted} updated={updated} unchanged={unchanged} "
        f"deleted={deleted} skipped={skipped}"
    )


def main() -> None:
    p = argparse.ArgumentParser(description="ESPN NBA scoreboard ingester")
    p.add_argument("--days-ahead", type=int, default=14)
    args = p.parse_args()
    run(days_ahead=args.days_ahead)


if __name__ == "__main__":
    main()
