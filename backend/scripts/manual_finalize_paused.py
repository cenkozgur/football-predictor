"""Manual finalize: copy live_* into ft_* for matches that are stuck at
status='paused' with non-null live scores AND a kickoff older than the
provided minimum age.

This is a recovery tool, not a recurring job. Use only when the daily
reaper has failed to settle a row because the provider's FINISHED
window dropped it before the live-scores worker observed the final-
whistle transition. We trust the live snapshot (which is what the user
saw on TV anyway) rather than waiting for a re-fetch that may never
come.

Idempotent. Skips matches that already have ft_home/ft_away. Skips
matches younger than --min-age-hours (default 6) so we never finalize
a still-running game that happens to be in a paused state.

Usage
-----
    python -m scripts.manual_finalize_paused          # default 6h cutoff
    python -m scripts.manual_finalize_paused --min-age-hours 12 --dry-run
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.db import SessionLocal
from app.models.match import Match


def run(min_age_hours: int = 6, dry_run: bool = False) -> int:
    cutoff = datetime.now(tz=timezone.utc).replace(tzinfo=None) - timedelta(hours=min_age_hours)
    finalized = 0
    skipped_no_live = 0
    with SessionLocal() as db:
        stmt = (
            select(Match)
            .options(joinedload(Match.home_team), joinedload(Match.away_team))
            .where(Match.status.in_(("paused", "in_play")))
            .where(Match.kickoff < cutoff)
        )
        rows = list(db.scalars(stmt).all())
        print(f"Found {len(rows)} paused/in_play matches older than {min_age_hours}h.")
        for m in rows:
            if m.ft_home is not None and m.ft_away is not None:
                continue
            if m.live_home is None or m.live_away is None:
                skipped_no_live += 1
                print(
                    f"  [{m.league}] {m.home_team.name} vs {m.away_team.name} "
                    f"({m.kickoff}) — paused but no live score, skipping"
                )
                continue
            print(
                f"  [{m.league}] {m.home_team.name} {m.live_home}-{m.live_away} "
                f"{m.away_team.name} ({m.kickoff}) — finalizing"
            )
            if not dry_run:
                m.ft_home = m.live_home
                m.ft_away = m.live_away
                m.status = "finished"
                m.live_minute = None
                m.live_home = None
                m.live_away = None
            finalized += 1
        if not dry_run:
            db.commit()
    print(f"Finalized {finalized} matches, {skipped_no_live} skipped (no live score).")
    return finalized


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--min-age-hours", type=int, default=6)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    run(min_age_hours=args.min_age_hours, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
