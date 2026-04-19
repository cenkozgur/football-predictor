"""One-shot migration: normalize season labels to a single format per league.

Context
-------
Different ingesters wrote the same real-world season under different labels:
  * football-data.co.uk CSV ingester → "2425" (YYMM, start/end year last-2-digits)
  * football-data.org API ingester → started out writing "2025" (start year only)
    which collides with the same label meaning different things in
    single-year-season leagues (NOR, SWE, FIN, IRL, where the season IS 2025).
  * An even earlier ingester → "2019/2020" (YYYY/YYYY) for some leagues.

The motivation/standings feature groups by (league, season), so this
fragmentation caused the 2025/26 season to split into two buckets and the
derived standings were empty for fresh fixtures — the whole point of the
motivation signal was nullified.

Policy
------
Two-year-season leagues: normalize everything to YYMM (e.g. "2425").
Single-year-season leagues: leave as YYYY.

Running the script
------------------
Idempotent — safe to run multiple times. Prints a summary of what it changed.
"""

from __future__ import annotations

import re
from collections import defaultdict

from sqlalchemy import select, update

from app.db import SessionLocal
from app.models.match import Match


# Leagues whose season spans two calendar years (August → May).
_TWO_YEAR_LEAGUES = {
    "E0", "E1", "D1", "I1", "SP1", "F1",
    "N1", "P1", "B1", "SC0", "T1", "G1",
    "AUT", "DNK", "POL", "ROU", "RUS", "SWZ",
}

# Single-year ("summer") leagues — season label is the calendar year itself.
# Kept as a list for documentation; we explicitly *skip* these rather than
# reformat them.
_SINGLE_YEAR_LEAGUES = {"FIN", "IRL", "NOR", "SWE"}


_YYYY_SLASH_YYYY = re.compile(r"^(\d{4})/(\d{4})$")
_YYYY = re.compile(r"^(\d{4})$")
_YYMM = re.compile(r"^(\d{4})$")  # same shape, but meaning is YYMM — use kickoff to disambiguate


def _to_yymm(yy_start: int) -> str:
    return f"{yy_start % 100:02d}{(yy_start + 1) % 100:02d}"


def _infer_start_year(label: str, sample_kickoff_month: int, sample_kickoff_year: int) -> int | None:
    """Figure out the start calendar year of a two-year season.

    For "2019/2020" it's obvious (2019). For "2025" (ambiguous — could be YYYY
    or YYMM) we look at a sample kickoff: if it's in Aug-Dec the start year
    is the kickoff year; if Jan-Jul it's kickoff year - 1.
    """
    m = _YYYY_SLASH_YYYY.match(label)
    if m:
        return int(m.group(1))
    # Four-digit label: ambiguous. For labels like "2425" we return None if
    # it already looks like YYMM (first-two and last-two differ by 1).
    m = _YYYY.match(label)
    if m:
        y = int(m.group(1))
        # If label looks like YYMM (e.g. 2425 → 24,25) don't migrate.
        first_two, last_two = y // 100, y % 100
        if last_two == first_two + 1 and first_two >= 10:
            return None
        # Plain YYYY (e.g. "2025"): infer from kickoff context.
        if sample_kickoff_month >= 7:
            return sample_kickoff_year
        return sample_kickoff_year - 1
    return None


def run() -> None:
    with SessionLocal() as db:
        # Snapshot: per (league, season) grab any kickoff so we can disambiguate.
        rows = db.execute(
            select(Match.league, Match.season, Match.kickoff)
        ).all()

        sample: dict[tuple[str, str], tuple[int, int]] = {}
        counts: dict[tuple[str, str], int] = defaultdict(int)
        for lg, sn, ko in rows:
            counts[(lg, sn)] += 1
            if (lg, sn) not in sample and ko is not None:
                sample[(lg, sn)] = (ko.month, ko.year)

        changes: list[tuple[str, str, str, int]] = []  # league, old_season, new_season, count

        for (lg, sn), cnt in counts.items():
            if lg in _SINGLE_YEAR_LEAGUES:
                # NOR/SWE/FIN/IRL already use YYYY which is the correct shape.
                continue
            if lg not in _TWO_YEAR_LEAGUES:
                # Unknown-policy leagues — leave them alone and log.
                continue
            ko_sample = sample.get((lg, sn))
            if ko_sample is None:
                continue
            start_year = _infer_start_year(sn, ko_sample[0], ko_sample[1])
            if start_year is None:
                continue
            new_label = _to_yymm(start_year)
            if new_label == sn:
                continue
            changes.append((lg, sn, new_label, cnt))

        if not changes:
            print("All season labels already normalized — nothing to do.")
            return

        print("Planned migrations:")
        for lg, old, new, cnt in sorted(changes):
            print(f"  {lg:5s} {old:12s} → {new}  ({cnt} rows)")

        # Apply. When the target label already exists in the table for the
        # same league, we need to merge into it rather than collide — but
        # since the uniqueness constraint is (home,away,kickoff) not season,
        # we can safely UPDATE every row's season label.
        for lg, old, new, _ in changes:
            db.execute(
                update(Match)
                .where(Match.league == lg, Match.season == old)
                .values(season=new)
            )
        db.commit()
        print(f"Applied {len(changes)} season-label migrations.")


if __name__ == "__main__":
    run()
