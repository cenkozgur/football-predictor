"""Fit per-market probability calibration curves on historical matches.

Why a separate fit step
-----------------------
Production track record alone (~36 settled legs by 2026-05-08) is too
small for a defensible calibration fit per market. Walking the model
forward over our 21k historical matches generates thousands of
(predicted_prob, did_it_happen) pairs per market — enough for an
isotonic fit that's stable rather than noisy.

What this writes
----------------
A `calibration_curves.json` next to the script's working directory.
The composer's prob_calibration module reads it on every request, so
publishing a new fit means just committing the new JSON.

Walk-forward strategy
---------------------
For speed (and because calibration drift is slow), we sample 1 in K
matches across the historical pool. For each sampled match:
    1. Fit Dixon-Coles on every match strictly before this one.
    2. Predict markets from the score matrix (1X2, OU lines, BTTS, DC).
    3. Record (raw_prob, actual_outcome) for every selection.

K is chosen so we end up with ~3000 samples per market — plenty for
isotonic without overfitting and fast enough to fit on a runner.

Usage
-----
    python scripts/fit_calibration.py
    python scripts/fit_calibration.py --sample-rate 30 --leagues E0,D1,I1
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db import SessionLocal
from app.ml.dixon_coles import DixonColesModel
from app.ml.markets import build_full_payload
from app.features.adjust import score_matrix_from_rates
from app.ml.prob_calibration import (
    CALIBRATION_PATH,
    CalibrationBundle,
    fit_curve,
    save_bundle,
)
from app.models.match import Match


def _did_outcome_hit(market: str, selection: str, ft_home: int, ft_away: int) -> bool:
    """Replicates resolve_coupons logic — kept inline to avoid import
    cycles and to make this script self-contained."""
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
        return ((total > line) and selection == "over") or (
            (total < line) and selection == "under"
        )
    if market == "btts":
        both = ft_home > 0 and ft_away > 0
        return (both and selection == "yes") or (not both and selection == "no")
    return False


def _build_training_df(prior: list[Match], asof: datetime) -> pd.DataFrame:
    if asof.tzinfo is None:
        asof = asof.replace(tzinfo=timezone.utc)
    rows = []
    for m in prior:
        if m.ft_home is None or m.ft_away is None:
            continue
        xh = m.xg_home if m.xg_home is not None else float(m.ft_home)
        xa = m.xg_away if m.xg_away is not None else float(m.ft_away)
        kickoff = m.kickoff
        if kickoff.tzinfo is None:
            kickoff = kickoff.replace(tzinfo=timezone.utc)
        rows.append({
            "home_team": m.home_team.name,
            "away_team": m.away_team.name,
            "home_goals": xh,
            "away_goals": xa,
            "league": m.league,
            "days_ago": max(0, (asof - kickoff).days),
        })
    return pd.DataFrame(rows)


def _flatten_payload_picks(payload: dict[str, Any]) -> list[tuple[str, str, float]]:
    """Yield (market, selection, prob) tuples from a market payload."""
    out = []
    if "1X2" in payload:
        for sel, p in payload["1X2"].items():
            out.append(("1X2", sel, float(p)))
    if "double_chance" in payload:
        for sel, p in payload["double_chance"].items():
            out.append(("double_chance", sel, float(p)))
    if "btts" in payload:
        for sel, p in payload["btts"].items():
            out.append(("btts", sel, float(p)))
    if "over_under" in payload:
        for line, ou in payload["over_under"].items():
            for sel, p in ou.items():
                out.append((f"over_under_{line}", sel, float(p)))
    return out


def run(
    leagues: list[str] | None = None,
    sample_rate: int = 25,
    refit_every: int = 100,
    min_train: int = 500,
) -> None:
    print(
        f"Calibration fit: sample_rate=1/{sample_rate}, "
        f"refit_every={refit_every}, min_train={min_train}"
    )
    with SessionLocal() as db:
        stmt = (
            select(Match)
            .where(Match.status == "finished")
            .options(selectinload(Match.home_team), selectinload(Match.away_team))
            .order_by(Match.kickoff.asc())
        )
        if leagues:
            stmt = stmt.where(Match.league.in_(leagues))
        matches = list(db.scalars(stmt).all())
        print(f"Total finished matches: {len(matches)}")

    samples_by_market: dict[str, list[tuple[float, bool]]] = defaultdict(list)
    model: DixonColesModel | None = None
    since_fit = refit_every

    for idx, m in enumerate(matches):
        # Walk-forward refit on a subset for speed.
        if since_fit >= refit_every:
            train_df = _build_training_df(matches[:idx], asof=m.kickoff)
            if len(train_df) < min_train:
                since_fit += 1
                continue
            try:
                model = DixonColesModel.fit(train_df, xi=0.0018, l2=2.0)
            except Exception as exc:  # noqa: BLE001
                print(f"  fit failed idx={idx}: {exc}")
                since_fit += 1
                continue
            since_fit = 0
            if (idx // refit_every) % 10 == 0:
                print(f"  fit at idx={idx}/{len(matches)} ({len(train_df)} rows)")
        else:
            since_fit += 1

        if model is None:
            continue
        # Sample-rate gate keeps the loop fast — calibration profile is
        # smooth so we don't need every match.
        if idx % sample_rate != 0:
            continue
        if m.home_team.name not in model.attack or m.away_team.name not in model.attack:
            continue

        base_lam, base_mu = model.rates(m.home_team.name, m.away_team.name)
        matrix = score_matrix_from_rates(base_lam, base_mu, model.rho)
        payload = build_full_payload(matrix)

        for market, selection, prob in _flatten_payload_picks(payload):
            hit = _did_outcome_hit(market, selection, m.ft_home, m.ft_away)
            samples_by_market[market].append((prob, hit))

    print()
    print(f"Sampled markets: {len(samples_by_market)}")
    bundle = CalibrationBundle(
        fitted_at=datetime.now(tz=timezone.utc).isoformat(),
        sample_size=sum(len(v) for v in samples_by_market.values()),
    )
    for market, pairs in sorted(samples_by_market.items()):
        probs = [p for p, _ in pairs]
        hits = [h for _, h in pairs]
        curve = fit_curve(probs, hits)
        bundle.curves[market] = curve
        actual_rate = sum(hits) / len(hits) if hits else 0
        avg_pred = sum(probs) / len(probs) if probs else 0
        print(
            f"  {market:25} n={len(pairs):5}  pred_avg={avg_pred:.3f}  "
            f"actual={actual_rate:.3f}  trained={curve.is_trained()}"
        )

    save_bundle(bundle)
    print()
    print(f"Wrote {CALIBRATION_PATH.resolve()}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--leagues", default=None)
    p.add_argument("--sample-rate", type=int, default=25)
    p.add_argument("--refit-every", type=int, default=100)
    p.add_argument("--min-train", type=int, default=500)
    args = p.parse_args()
    leagues = args.leagues.split(",") if args.leagues else None
    run(
        leagues=leagues,
        sample_rate=args.sample_rate,
        refit_every=args.refit_every,
        min_train=args.min_train,
    )


if __name__ == "__main__":
    main()
