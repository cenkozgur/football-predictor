"""Coupon suggestion endpoints.

GET /coupons — returns confidence-ranked accumulator suggestions for today's
              (or any date range's) upcoming matches.

The logic lives in `app.ml.coupons.suggest_coupons`; this route just pulls the
latest prediction per upcoming match from the DB and passes everything through.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.db import get_db
from app.ml.coupons import suggest_coupons
from app.models.match import Match
from app.models.prediction import Prediction

router = APIRouter()


ALLOWED_MARKET_SET = {
    "1X2",
    "double_chance",
    "over_under",
    "btts",
    "odd_even",
    "correct_score",
    # "asian_handicap", "home_over_under", "away_over_under"  # niche, off by default
}


@router.get("")
def list_coupon_suggestions(
    min_prob: float = Query(default=0.65, ge=0.01, le=0.99),
    legs: int = Query(default=3, ge=1, le=6),
    markets: str | None = Query(
        default=None,
        description="Comma-separated market filter, e.g. '1X2,btts'. Default: all main markets.",
    ),
    limit_matches: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return coupon suggestions for upcoming matches with predictions."""

    now_naive = datetime.now(tz=timezone.utc).replace(tzinfo=None)

    # Join: upcoming matches that have a prediction (latest per match)
    stmt = (
        select(Match, Prediction)
        .join(Prediction, Prediction.match_id == Match.id)
        .options(joinedload(Match.home_team), joinedload(Match.away_team))
        .where(Match.status == "scheduled")
        .where(Match.kickoff > now_naive)
        .order_by(Match.kickoff.asc())
        .limit(limit_matches)
    )

    rows = db.execute(stmt).all()

    # Per match, keep only the newest prediction (the join above returns all
    # predictions, but in production we upsert to one row per match).
    seen: set[int] = set()
    match_predictions: list[dict[str, Any]] = []
    for match, pred in rows:
        if match.id in seen:
            continue
        seen.add(match.id)
        match_predictions.append(
            {
                "match_id": match.id,
                "home_team": match.home_team.name,
                "away_team": match.away_team.name,
                "kickoff": match.kickoff.isoformat(),
                "league": match.league,
                "payload": pred.payload,
            }
        )

    if markets:
        allowed = {m.strip() for m in markets.split(",") if m.strip()}
    else:
        allowed = ALLOWED_MARKET_SET

    result = suggest_coupons(
        match_predictions,
        min_prob_per_leg=min_prob,
        num_legs=legs,
        allowed_markets=allowed,
    )

    result["counts"] = {
        "matches_considered": len(match_predictions),
        "qualifying_picks": len(result["all_picks"]),
    }
    # Unified array so UIs can render a single list (primary first, then alts).
    coupons_list = []
    if result.get("primary"):
        coupons_list.append(result["primary"])
    coupons_list.extend(result.get("alternatives", []))
    result["coupons"] = coupons_list
    return result
