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


def run(days_ahead: int) -> None:
    today = datetime.now(tz=timezone.utc).date()
    days = [today + timedelta(days=i) for i in range(days_ahead + 1)]
    print(f"Fetching ESPN NBA scoreboard for {len(days)} days ({days[0]} → {days[-1]})…")

    inserted = updated = unchanged = 0
    skipped = 0

    with _client() as client, SessionLocal() as session:
        for d in days:
            date_str = d.strftime("%Y%m%d")
            try:
                games = fetch_day(client, date_str)
            except httpx.HTTPStatusError as e:
                print(f"  [{date_str}] HTTP {e.response.status_code}")
                continue
            except httpx.HTTPError as e:
                print(f"  [{date_str}] network error: {e}")
                continue

            for g in games:
                parsed = parse_event(g)
                if parsed is None:
                    skipped += 1
                    continue
                ext_ref, fields = parsed
                outcome = upsert(session, ext_ref, fields)
                if outcome == "inserted":
                    inserted += 1
                elif outcome == "updated":
                    updated += 1
                else:
                    unchanged += 1

        session.commit()

    print(
        f"Done. inserted={inserted} updated={updated} unchanged={unchanged} skipped={skipped}"
    )


def main() -> None:
    p = argparse.ArgumentParser(description="ESPN NBA scoreboard ingester")
    p.add_argument("--days-ahead", type=int, default=14)
    args = p.parse_args()
    run(days_ahead=args.days_ahead)


if __name__ == "__main__":
    main()
