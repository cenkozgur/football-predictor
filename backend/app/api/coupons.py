"""Coupon suggestion endpoints.

GET /coupons — returns confidence-ranked accumulator suggestions for today's
              (or any date range's) upcoming matches.

The logic lives in `app.ml.coupons.suggest_coupons`; this route just pulls the
latest prediction per upcoming match from the DB and passes everything through.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.db import get_db
from app.ml.coupons import suggest_coupons, suggest_hit_probability_variants
from app.models.match import Match
from app.models.odds import Odds
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
    min_prob: float = Query(default=0.55, ge=0.01, le=0.99),
    legs: int = Query(
        default=3,
        ge=1,
        le=6,
        description="Preferred leg count. If not enough edge-positive picks exist, composer falls back to smaller coupons down to min_legs.",
    ),
    min_legs: int = Query(default=1, ge=1, le=6),
    max_legs: int = Query(default=4, ge=1, le=6),
    markets: str | None = Query(
        default=None,
        description="Comma-separated market filter, e.g. '1X2,btts'. Default: all main markets.",
    ),
    limit_matches: int = Query(default=200, ge=1, le=500),
    days_ahead: int = Query(
        default=2,
        ge=1,
        le=14,
        description="Only consider matches kicking off within this many days. Defaults to 2 so coupons reflect today/tomorrow, not fixtures two weeks out.",
    ),
    min_combined_odds: float = Query(
        default=1.6,
        ge=1.0,
        le=50.0,
        description="Composer rejects coupons below this combined odds target (1.6 ≈ meaningful payout). Set to 1.0 to allow banker-only coupons.",
    ),
    diversify_markets: bool = Query(
        default=True,
        description="Forbid two legs from the same base market (prevents all-Alt/Üst coupons).",
    ),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return coupon suggestions for upcoming matches with predictions."""

    now_naive = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    horizon = now_naive + timedelta(days=days_ahead)

    # Join: upcoming matches that have a prediction (latest per match)
    stmt = (
        select(Match, Prediction)
        .join(Prediction, Prediction.match_id == Match.id)
        .options(joinedload(Match.home_team), joinedload(Match.away_team))
        .where(Match.status == "scheduled")
        .where(Match.kickoff > now_naive)
        .where(Match.kickoff <= horizon)
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

    # Pull odds for all candidate matches in one query; index as
    # {match_id: {(market, selection): best_decimal_odds}}.
    # We prefer closing odds (B365C > PSC) when multiple sources exist, because
    # closing odds embed the most market information and make for the cleanest
    # value-edge signal.
    source_priority = {"B365C": 0, "PSC": 1, "B365": 2, "PS": 3, "WH": 4}
    match_ids = [mp["match_id"] for mp in match_predictions]
    odds_rows = (
        db.query(Odds).filter(Odds.match_id.in_(match_ids)).all() if match_ids else []
    )
    odds_by_match: dict[int, dict[tuple[str, str], float]] = {}
    odds_source_seen: dict[int, dict[tuple[str, str], int]] = {}
    for o in odds_rows:
        # Normalize market naming: CSV ingester stores "OU_2.5" / "1X2";
        # coupon engine emits "over_under_2.5" / "1X2". We store both forms.
        keys: list[tuple[str, str]] = [(o.market, o.selection)]
        if o.market.startswith("OU_"):
            keys.append((f"over_under_{o.market[3:]}", o.selection))
        for key in keys:
            prio = source_priority.get(o.source, 99)
            prev_prio = odds_source_seen.setdefault(o.match_id, {}).get(key, 99)
            if prio <= prev_prio:
                odds_by_match.setdefault(o.match_id, {})[key] = float(o.decimal_odds)
                odds_source_seen[o.match_id][key] = prio

    result = suggest_coupons(
        match_predictions,
        min_prob_per_leg=min_prob,
        num_legs=legs,
        min_legs=min_legs,
        max_legs=max_legs,
        allowed_markets=allowed,
        min_combined_odds=min_combined_odds,
        enforce_market_diversity=diversify_markets,
        odds_by_match=odds_by_match,
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


def _load_match_predictions_with_odds(db: Session, days_ahead: int, limit: int):
    """Shared loader: upcoming matches + latest prediction + odds index.

    Returns (match_predictions: list, odds_by_match: dict). Used by both the
    edge-gated /coupons route and the hit-probability /coupons/variations
    route so they see the exact same input.
    """
    now_naive = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    horizon = now_naive + timedelta(days=days_ahead)

    stmt = (
        select(Match, Prediction)
        .join(Prediction, Prediction.match_id == Match.id)
        .options(joinedload(Match.home_team), joinedload(Match.away_team))
        .where(Match.status == "scheduled")
        .where(Match.kickoff > now_naive)
        .where(Match.kickoff <= horizon)
        .order_by(Match.kickoff.asc())
        .limit(limit)
    )
    rows = db.execute(stmt).all()

    seen: set[int] = set()
    match_predictions: list[dict[str, Any]] = []
    for match, pred in rows:
        if match.id in seen:
            continue
        seen.add(match.id)
        match_predictions.append({
            "match_id": match.id,
            "home_team": match.home_team.name,
            "away_team": match.away_team.name,
            "kickoff": match.kickoff.isoformat(),
            "league": match.league,
            "payload": pred.payload,
        })

    # Same odds priority order as /coupons.
    source_priority = {"B365C": 0, "PSC": 1, "B365": 2, "PS": 3, "WH": 4, "AFP": 50}
    match_ids = [mp["match_id"] for mp in match_predictions]
    odds_rows = (
        db.query(Odds).filter(Odds.match_id.in_(match_ids)).all() if match_ids else []
    )
    odds_by_match: dict[int, dict[tuple[str, str], float]] = {}
    odds_source_seen: dict[int, dict[tuple[str, str], int]] = {}
    for o in odds_rows:
        keys: list[tuple[str, str]] = [(o.market, o.selection)]
        if o.market.startswith("OU_"):
            keys.append((f"over_under_{o.market[3:]}", o.selection))
        for key in keys:
            prio = source_priority.get(o.source, 99)
            prev = odds_source_seen.setdefault(o.match_id, {}).get(key, 99)
            if prio <= prev:
                odds_by_match.setdefault(o.match_id, {})[key] = float(o.decimal_odds)
                odds_source_seen[o.match_id][key] = prio

    return match_predictions, odds_by_match


@router.get("/variations")
def coupon_variations(
    legs: int = Query(default=2, ge=1, le=5),
    min_prob_per_leg: float = Query(default=0.50, ge=0.30, le=0.95),
    markets: str | None = Query(
        default=None,
        description="Comma-separated market filter, default all main markets.",
    ),
    days_ahead: int = Query(default=2, ge=1, le=14),
    limit_matches: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Hit-probability coupon variations (Güvenli / Dengeli / Cesur).

    Different question from /coupons: instead of 'where does the model beat
    the bookmaker', this asks 'which 2-3 leg combination is most likely to
    actually hit, in three different payout bands'. No edge gate, no
    per-league policy — pure probability × odds tradeoff. The user who
    bets on bilyoner anyway wants this answer, not the academic one.
    """
    match_predictions, odds_by_match = _load_match_predictions_with_odds(
        db, days_ahead=days_ahead, limit=limit_matches
    )

    if markets:
        allowed = {m.strip() for m in markets.split(",") if m.strip()}
    else:
        allowed = ALLOWED_MARKET_SET

    result = suggest_hit_probability_variants(
        match_predictions,
        num_legs=legs,
        min_prob_per_leg=min_prob_per_leg,
        allowed_markets=allowed,
        odds_by_match=odds_by_match,
    )
    return result
