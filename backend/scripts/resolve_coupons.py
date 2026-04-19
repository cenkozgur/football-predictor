"""Resolve recorded coupons against finished match scores.

Fills `coupon_legs.hit` for every leg whose underlying match has finished,
and `coupons.hit` once *every* leg in a coupon is resolved. Safe to run
repeatedly — legs already resolved are skipped, never flipped.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db import SessionLocal
from app.models.coupon import Coupon, CouponLeg
from app.models.match import Match


def _did_leg_hit(market: str, selection: str, ft_home: int, ft_away: int) -> bool:
    """Shared leg-resolution logic. Mirrors stats._did_pick_hit but duplicated
    here so the history pipeline doesn't reach into a FastAPI route module."""
    total = ft_home + ft_away
    if market == "1X2":
        actual = "1" if ft_home > ft_away else ("2" if ft_home < ft_away else "X")
        return selection == actual
    if market == "double_chance":
        r = "1" if ft_home > ft_away else ("2" if ft_home < ft_away else "X")
        if r == "1":
            return selection in ("1X", "12")
        if r == "2":
            return selection in ("12", "X2")
        return selection in ("1X", "X2")
    if market.startswith("over_under_"):
        try:
            line = float(market.split("_", 2)[2])
        except (ValueError, IndexError):
            return False
        actual = "over" if total > line else "under"
        return selection == actual
    if market == "btts":
        return ((ft_home > 0 and ft_away > 0) and selection == "yes") or (
            (ft_home == 0 or ft_away == 0) and selection == "no"
        )
    if market == "odd_even":
        return (total % 2 == 1 and selection == "odd") or (
            total % 2 == 0 and selection == "even"
        )
    if market == "correct_score":
        return selection == f"{ft_home}-{ft_away}"
    return False


def run() -> dict[str, int]:
    resolved_legs = 0
    resolved_coupons = 0
    still_pending = 0

    with SessionLocal() as db:
        unresolved = (
            db.scalars(
                select(Coupon)
                .where(Coupon.hit.is_(None))
                .options(selectinload(Coupon.legs))
            )
            .all()
        )

        for coupon in unresolved:
            pending = 0
            for leg in coupon.legs:
                if leg.hit is not None:
                    continue
                match = db.get(Match, leg.match_id)
                if match is None or match.ft_home is None or match.ft_away is None:
                    pending += 1
                    continue
                leg.hit = _did_leg_hit(leg.market, leg.selection, match.ft_home, match.ft_away)
                resolved_legs += 1

            # If every leg now has a resolution, the coupon itself resolves.
            if pending == 0 and all(leg.hit is not None for leg in coupon.legs):
                coupon.hit = all(leg.hit for leg in coupon.legs)
                coupon.resolved_at = datetime.now(tz=timezone.utc)
                resolved_coupons += 1
            else:
                still_pending += 1

        db.commit()

    print(
        f"Resolved {resolved_legs} legs, finalized {resolved_coupons} coupons "
        f"({still_pending} still pending)."
    )
    return {
        "legs_resolved": resolved_legs,
        "coupons_finalized": resolved_coupons,
        "still_pending": still_pending,
    }


if __name__ == "__main__":
    run()
