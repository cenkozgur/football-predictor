"""Snapshot today's composer output into the coupons/coupon_legs tables.

The live /coupons endpoint recomputes suggestions on demand, which is great
for freshness but means nothing is persisted — we cannot look back and say
"which coupons did we suggest last Tuesday, and how many hit?". This script
closes that gap: once a day, after ingest, it runs the same composer the API
does and writes the results to the history tables. `resolve_coupons.py` (run
after a day's matches finish) flips each leg's hit flag.

Idempotent by (date, signature): running this twice on the same day — e.g.
because the ingest workflow retried — won't create duplicate rows.
"""

from __future__ import annotations

import argparse
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.db import SessionLocal
from app.ml.coupons import suggest_coupons
from app.models.coupon import Coupon, CouponLeg
from app.models.match import Match
from app.models.odds import Odds
from app.models.prediction import Prediction


# Mirror the API's defaults so the snapshot matches what users would have seen
# had they hit /coupons at this moment.
_DEFAULTS = dict(
    min_prob_per_leg=0.55,
    num_legs=3,
    min_legs=1,
    max_legs=4,
    min_combined_odds=1.6,
    enforce_market_diversity=True,
)
_ALLOWED_MARKETS = {
    "1X2",
    "double_chance",
    "over_under",
    "btts",
    "odd_even",
    "correct_score",
}
_SOURCE_PRIORITY = {"B365C": 0, "PSC": 1, "B365": 2, "PS": 3, "WH": 4}


def _signature(legs: list[dict[str, Any]]) -> str:
    """Stable hash of this coupon's legs so duplicates collapse."""
    parts = sorted(
        f"{leg['match_id']}|{leg['market']}|{leg['selection']}" for leg in legs
    )
    return hashlib.sha256("||".join(parts).encode()).hexdigest()[:32]


def _load_match_predictions(db, horizon_days: int) -> list[dict[str, Any]]:
    """Same join the /coupons route uses — upcoming matches with predictions."""
    now_naive = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    horizon = now_naive + timedelta(days=horizon_days)
    stmt = (
        select(Match, Prediction)
        .join(Prediction, Prediction.match_id == Match.id)
        .options(joinedload(Match.home_team), joinedload(Match.away_team))
        .where(Match.status == "scheduled")
        .where(Match.kickoff > now_naive)
        .where(Match.kickoff <= horizon)
        .order_by(Match.kickoff.asc())
    )
    rows = db.execute(stmt).all()
    seen: set[int] = set()
    out = []
    for match, pred in rows:
        if match.id in seen:
            continue
        seen.add(match.id)
        out.append(
            {
                "match_id": match.id,
                "home_team": match.home_team.name,
                "away_team": match.away_team.name,
                "kickoff": match.kickoff.isoformat(),
                "league": match.league,
                "payload": pred.payload,
            }
        )
    return out


def _build_odds_index(db, match_ids: list[int]) -> dict[int, dict[tuple[str, str], float]]:
    """Same closing-odds-preferred index used by /coupons."""
    if not match_ids:
        return {}
    odds_rows = db.query(Odds).filter(Odds.match_id.in_(match_ids)).all()
    by_match: dict[int, dict[tuple[str, str], float]] = {}
    seen: dict[int, dict[tuple[str, str], int]] = {}
    for o in odds_rows:
        keys: list[tuple[str, str]] = [(o.market, o.selection)]
        if o.market.startswith("OU_"):
            keys.append((f"over_under_{o.market[3:]}", o.selection))
        for key in keys:
            prio = _SOURCE_PRIORITY.get(o.source, 99)
            prev = seen.setdefault(o.match_id, {}).get(key, 99)
            if prio <= prev:
                by_match.setdefault(o.match_id, {})[key] = float(o.decimal_odds)
                seen[o.match_id][key] = prio
    return by_match


def _save_coupon(db, today: str, coupon_dict: dict[str, Any], kind: str) -> bool:
    """Insert one coupon + its legs. Returns True if inserted, False if dup."""
    legs = coupon_dict.get("legs", [])
    if not legs:
        return False
    sig = _signature(legs)

    # Duplicate check — same day + same signature
    existing = db.scalar(
        select(Coupon).where(Coupon.generated_on == today, Coupon.signature == sig)
    )
    if existing:
        return False

    avg_composite = sum(leg.get("composite", 0.0) for leg in legs) / len(legs)
    coupon = Coupon(
        generated_on=today,
        signature=sig,
        kind=kind,
        num_legs=len(legs),
        combined_prob=float(coupon_dict.get("combined_prob", 0.0)),
        combined_odds=float(coupon_dict.get("combined_odds") or 0.0) or None,
        avg_composite=avg_composite,
    )
    for leg in legs:
        coupon.legs.append(
            CouponLeg(
                match_id=leg["match_id"],
                market=leg["market"],
                selection=leg["selection"],
                selection_label=leg.get("selection_label", leg["selection"]),
                prob=float(leg["prob"]),
                book_odds=leg.get("book_odds"),
                value_edge=leg.get("value_edge"),
                composite=leg.get("composite"),
            )
        )
    db.add(coupon)
    return True


def run(horizon_days: int = 2) -> int:
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    with SessionLocal() as db:
        matches = _load_match_predictions(db, horizon_days=horizon_days)
        if not matches:
            print(f"[{today}] No upcoming matches with predictions — nothing to snapshot.")
            return 0

        odds_by_match = _build_odds_index(db, [m["match_id"] for m in matches])
        result = suggest_coupons(
            matches,
            allowed_markets=_ALLOWED_MARKETS,
            odds_by_match=odds_by_match,
            **_DEFAULTS,
        )

        inserted = 0
        if result.get("primary"):
            primary_kind = "fallback" if result["primary"].get("is_fallback") else "primary"
            if _save_coupon(db, today, result["primary"], kind=primary_kind):
                inserted += 1
        for alt in result.get("alternatives", []):
            if _save_coupon(db, today, alt, kind="alternative"):
                inserted += 1

        db.commit()
        print(
            f"[{today}] Snapshotted {inserted} new coupons "
            f"(primary + {len(result.get('alternatives', []))} alternatives offered, "
            f"{len(matches)} matches considered)."
        )
        return inserted


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--days-ahead",
        type=int,
        default=2,
        help="Match horizon in days, matches /coupons default.",
    )
    args = p.parse_args()
    run(horizon_days=args.days_ahead)


if __name__ == "__main__":
    main()
