"""One-shot reset: wipe user-facing history while preserving the model's fuel.

Philosophy
----------
The 6-source prediction engine (Dixon-Coles + xG + form + motivation +
availability + value) is trained on every finished match in the DB. Deleting
those rows would kill the engine. But the user wants a clean slate: no old
coupons, no stale 'scheduled' rows rotting in the Yaklaşan tab, no
predictions from the pre-xG era leaking into /stats/accuracy.

So this script draws the line carefully:

    KEEP (model fuel):
        - matches.status='finished' rows (ft_home/ft_away/xg_home/xg_away)
        - teams, odds, team_availability
        - matches.status='scheduled' rows with future kickoff

    WIPE (user-facing remnants):
        - predictions (all rows — next predict_upcoming regenerates)
        - coupons + coupon_legs (all rows — recorder starts fresh)
        - matches.status='scheduled' with past kickoff (UI cleanup;
          these are the stragglers the reaper couldn't settle)

Safety
------
Guarded behind `--confirm` so a stray invocation doesn't nuke anything.
Run once manually via workflow_dispatch, not on a recurring cron.

Usage
-----
    python -m scripts.reset_user_facing_history --confirm
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

from sqlalchemy import delete, select

from app.db import SessionLocal
from app.models.coupon import Coupon, CouponLeg
from app.models.match import Match
from app.models.prediction import Prediction


def run(confirm: bool = False) -> None:
    if not confirm:
        print("Refusing to run without --confirm. This deletes predictions, coupons,")
        print("and past-kickoff 'scheduled' match rows. Re-run with --confirm to proceed.")
        return

    now_naive = datetime.now(tz=timezone.utc).replace(tzinfo=None)

    with SessionLocal() as db:
        # Count before, so the log shows what actually got removed.
        n_preds = db.scalar(select(Prediction).with_only_columns(Prediction.id).limit(1))
        pred_count = db.query(Prediction).count()
        coupon_count = db.query(Coupon).count()
        leg_count = db.query(CouponLeg).count()

        stale_q = (
            select(Match)
            .where(Match.status == "scheduled")
            .where(Match.kickoff < now_naive)
        )
        stale_count = len(list(db.scalars(stale_q).all()))

        # CouponLeg has ON DELETE CASCADE via the relationship, but delete()
        # bypasses ORM-level cascades — wipe legs explicitly first.
        db.execute(delete(CouponLeg))
        db.execute(delete(Coupon))
        db.execute(delete(Prediction))

        # Past-kickoff scheduled: UI cleanup only. We don't touch finished
        # rows because those are Dixon-Coles training data.
        db.execute(
            delete(Match)
            .where(Match.status == "scheduled")
            .where(Match.kickoff < now_naive)
        )

        # Sanity check: finished match count should be untouched.
        finished_count = db.query(Match).filter(Match.status == "finished").count()
        future_scheduled = (
            db.query(Match)
            .filter(Match.status == "scheduled", Match.kickoff >= now_naive)
            .count()
        )

        db.commit()

        print("User-facing history reset complete.")
        print(f"  Deleted: {pred_count} predictions")
        print(f"  Deleted: {coupon_count} coupons, {leg_count} coupon_legs")
        print(f"  Deleted: {stale_count} past-kickoff scheduled matches")
        print(f"  Preserved: {finished_count} finished matches (model fuel)")
        print(f"  Preserved: {future_scheduled} future scheduled matches")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--confirm",
        action="store_true",
        help="Required; without it the script refuses to delete anything.",
    )
    args = p.parse_args()
    run(confirm=args.confirm)


if __name__ == "__main__":
    main()
