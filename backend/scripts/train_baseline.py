"""Fit a Dixon-Coles baseline on already-ingested matches, then print a sample prediction.

Usage:
    python scripts/train_baseline.py --league E0 --season 2324
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import select

from app.db import SessionLocal
from app.ml.dixon_coles import DixonColesModel
from app.ml.markets import build_full_payload
from app.models.match import Match


def load_matches(league: str, season: str | None = None) -> pd.DataFrame:
    with SessionLocal() as db:
        stmt = (
            select(Match)
            .where(Match.league == league)
            .where(Match.status == "finished")
        )
        if season:
            stmt = stmt.where(Match.season == season)
        rows = db.scalars(stmt).all()

        records: list[dict] = []
        today = datetime.now(tz=timezone.utc)
        for m in rows:
            records.append(
                {
                    "home_team": m.home_team.name,
                    "away_team": m.away_team.name,
                    "home_goals": m.ft_home,
                    "away_goals": m.ft_away,
                    "days_ago": max(0, (today - m.kickoff).days),
                }
            )
    return pd.DataFrame.from_records(records)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fit Dixon-Coles on stored matches.")
    parser.add_argument("--league", required=True)
    parser.add_argument("--season", default=None)
    parser.add_argument(
        "--xi", type=float, default=0.0018, help="Time-decay parameter (0 disables)."
    )
    parser.add_argument("--home", default=None, help="Sample prediction home team")
    parser.add_argument("--away", default=None, help="Sample prediction away team")
    args = parser.parse_args()

    df = load_matches(args.league, args.season)
    if df.empty:
        raise SystemExit(f"No matches found for league={args.league} season={args.season}")
    print(f"Loaded {len(df)} matches. Fitting Dixon-Coles...")

    model = DixonColesModel.fit(df, xi=args.xi)
    print(f"Fit complete. home_adv={model.home_adv:.3f}  rho={model.rho:.3f}")
    print(f"Teams: {len(model.teams)}")

    top_attack = sorted(model.attack.items(), key=lambda kv: kv[1], reverse=True)[:5]
    top_defense = sorted(model.defense.items(), key=lambda kv: kv[1])[:5]
    print("\nStrongest attack:")
    for t, a in top_attack:
        print(f"  {t:30s}  alpha={a:+.3f}")
    print("Strongest defense (lower = better):")
    for t, b in top_defense:
        print(f"  {t:30s}  beta={b:+.3f}")

    if args.home and args.away:
        matrix = model.score_matrix(args.home, args.away)
        payload = build_full_payload(matrix)
        print(f"\nPrediction: {args.home} vs {args.away}")
        print(json.dumps(payload, indent=2, default=float))


if __name__ == "__main__":
    main()
