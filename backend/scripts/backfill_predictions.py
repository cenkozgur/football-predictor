"""Walk-forward backfill of predictions on finished matches.

Same discipline as scripts/backtest.py but persists a full multi-market payload
into the `predictions` table so the /stats/accuracy endpoint can evaluate them.

For each target match, the model is fit on STRICTLY prior matches — no data
leakage — and refit every `refit_every` matches to keep runtime bounded.

Usage
-----
    # Backfill every league (pooled model)
    python scripts/backfill_predictions.py

    # Faster preview (single league + fewer refits)
    python scripts/backfill_predictions.py --leagues E0 --refit-every 100

    # Adjust the warmup / refit cadence
    python scripts/backfill_predictions.py --min-train 500 --refit-every 100
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from app.db import SessionLocal
from app.ml.dixon_coles import DixonColesModel
from app.ml.markets import build_full_payload
from app.models.match import Match
from app.models.prediction import Prediction


MODEL_L2 = 2.0
MODEL_XI = 0.0018


def _load_finished(db, leagues: list[str] | None) -> list[Match]:
    stmt = (
        select(Match)
        .where(Match.status == "finished")
        .where(Match.ft_home.is_not(None))
        .options(selectinload(Match.home_team), selectinload(Match.away_team))
        .order_by(Match.kickoff.asc())
    )
    if leagues:
        stmt = stmt.where(Match.league.in_(leagues))
    return list(db.scalars(stmt).all())


def run(
    leagues: list[str] | None,
    min_train: int,
    refit_every: int,
    skip_existing: bool,
) -> int:
    with SessionLocal() as db:
        matches = _load_finished(db, leagues)
        if len(matches) <= min_train:
            print(
                f"Only {len(matches)} finished matches — need at least {min_train + 1}."
            )
            return 0

        print(
            f"Loaded {len(matches)} finished matches across "
            f"{len({m.league for m in matches})} leagues. "
            f"Walk-forward from index {min_train}, refit every {refit_every}."
        )

        if skip_existing:
            existing = set(
                db.scalars(
                    select(Prediction.match_id).where(
                        Prediction.match_id.in_([m.id for m in matches])
                    )
                ).all()
            )
            print(f"  skipping {len(existing)} matches that already have predictions")
        else:
            existing = set()

        records = [
            {
                "kickoff": m.kickoff,
                "home_team": m.home_team.name,
                "away_team": m.away_team.name,
                "home_goals": m.ft_home,
                "away_goals": m.ft_away,
                "league": m.league,
            }
            for m in matches
        ]
        df_all = pd.DataFrame.from_records(records)

        model: DixonColesModel | None = None
        last_refit = -1
        written = 0
        skipped_unknown = 0
        skipped_existing = 0
        t_start = time.time()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")

        for i in range(min_train, len(matches)):
            m = matches[i]

            if m.id in existing:
                skipped_existing += 1
                continue

            if model is None or i - last_refit >= refit_every:
                train_df = df_all.iloc[:i].copy()
                test_kickoff = m.kickoff
                train_df["days_ago"] = (test_kickoff - train_df["kickoff"]).dt.days
                train_df = train_df.drop(columns=["kickoff"])
                try:
                    model = DixonColesModel.fit(train_df, xi=MODEL_XI, l2=MODEL_L2)
                except Exception as exc:  # noqa: BLE001
                    print(f"  fit failed at i={i}: {exc}")
                    continue
                last_refit = i
                elapsed = time.time() - t_start
                print(
                    f"  refit i={i}/{len(matches)} teams={len(model.teams)} "
                    f"rho={model.rho:+.3f}  (elapsed {elapsed:.0f}s, "
                    f"written {written})"
                )

            home_name = m.home_team.name
            away_name = m.away_team.name
            if home_name not in model.attack or away_name not in model.attack:
                skipped_unknown += 1
                continue

            matrix = model.score_matrix(home_name, away_name)
            payload = build_full_payload(matrix)
            lam, mu = model.rates(home_name, away_name)

            model_version = f"dc-backfill-l2_{MODEL_L2}-xi_{MODEL_XI}-i_{last_refit}-{stamp}"

            db.execute(delete(Prediction).where(Prediction.match_id == m.id))
            db.add(
                Prediction(
                    match_id=m.id,
                    model_version=model_version,
                    payload=payload,
                    lambda_home=float(lam),
                    lambda_away=float(mu),
                )
            )
            written += 1

            # Commit in chunks so a crash doesn't lose everything.
            if written % 500 == 0:
                db.commit()

        db.commit()
        elapsed = time.time() - t_start
        print(
            f"\nDone in {elapsed:.0f}s. Wrote {written} predictions "
            f"({skipped_unknown} unknown-team, {skipped_existing} already had one)."
        )
        return written


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Walk-forward prediction backfill on finished matches."
    )
    parser.add_argument("--leagues", default=None, help="Comma-separated league codes.")
    parser.add_argument("--min-train", type=int, default=500)
    parser.add_argument("--refit-every", type=int, default=100)
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip matches that already have a prediction row (faster re-runs).",
    )
    args = parser.parse_args()

    leagues = args.leagues.split(",") if args.leagues else None
    run(
        leagues=leagues,
        min_train=args.min_train,
        refit_every=args.refit_every,
        skip_existing=args.skip_existing,
    )


if __name__ == "__main__":
    main()
