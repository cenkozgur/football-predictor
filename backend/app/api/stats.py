"""Accuracy / hit-rate tracking for past predictions.

Two views:
- /stats/accuracy — raw per-market argmax hit rates. Good for model calibration
  debugging; misleading as a "are we winning" proxy because it includes 0.5 Üst
  which is always ~93% and never makes it into a real coupon.
- /stats/strategy — applies the *live* coupon composer to finished matches and
  reports only on picks that would have actually been selected. This is the
  honest number: if our composite-signal strategy picks a bet, how often does
  it land?
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.db import get_db
from app.ml.accuracy import evaluate_predictions, summarize
from app.ml.coupons import _enumerate_picks, _base_market, _compose_coupon
from app.models.match import Match
from app.models.odds import Odds
from app.models.prediction import Prediction

router = APIRouter()


@router.get("/accuracy")
def accuracy(
    league: str | None = Query(default=None),
    limit: int = Query(default=2000, ge=1, le=25000),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Evaluate model predictions on finished matches."""
    stmt = (
        select(Match, Prediction)
        .join(Prediction, Prediction.match_id == Match.id)
        .options(joinedload(Match.home_team), joinedload(Match.away_team))
        .where(Match.ft_home.is_not(None))
        .where(Match.ft_away.is_not(None))
        .order_by(Match.kickoff.desc())
        .limit(limit)
    )
    if league:
        stmt = stmt.where(Match.league == league)

    rows = db.execute(stmt).all()

    seen: set[int] = set()
    items = []
    for match, pred in rows:
        if match.id in seen:
            continue
        seen.add(match.id)
        items.append((
            match.id,
            match.kickoff.isoformat(),
            match.league,
            match.home_team.name,
            match.away_team.name,
            match.ft_home,
            match.ft_away,
            pred.payload,
        ))

    eval_rows = evaluate_predictions(items)
    summary = summarize(eval_rows)

    # Also return recent per-pick details (newest-first by kickoff)
    details = [
        {
            "match_id": r.match_id,
            "kickoff": r.kickoff,
            "league": r.league,
            "home_team": r.home_team,
            "away_team": r.away_team,
            "score": f"{r.ft_home}-{r.ft_away}",
            "market": r.market,
            "pick": r.pick,
            "pick_prob": r.pick_prob,
            "actual": r.actual,
            "hit": r.hit,
        }
        for r in eval_rows
    ]
    details.sort(key=lambda d: d["kickoff"], reverse=True)

    summary["matches_evaluated"] = len(items)
    summary["details"] = details[:200]

    # Spec-compatible aliases so UIs using the documented field names work:
    summary["markets"] = summary.get("by_market", {})
    compat_cal = []
    for c in summary.get("calibration", []):
        compat_cal.append({
            **c,
            "bin": c.get("range"),
            "expected": c.get("avg_prob"),
            "actual": c.get("hit_rate"),
            "n": c.get("picks"),
        })
    if compat_cal:
        summary["calibration"] = compat_cal
    return summary


def _did_pick_hit(market: str, selection: str, ft_home: int, ft_away: int) -> bool:
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
        actual = "yes" if (ft_home > 0 and ft_away > 0) else "no"
        return selection == actual
    if market == "odd_even":
        actual = "odd" if total % 2 == 1 else "even"
        return selection == actual
    return False


_STRATEGY_MARKETS = {"1X2", "double_chance", "over_under", "btts", "odd_even"}


@router.get("/strategy")
def strategy_accuracy(
    league: str | None = Query(default=None),
    limit: int = Query(default=5000, ge=1, le=25000),
    min_prob: float = Query(default=0.55, ge=0.01, le=0.99),
    num_legs: int = Query(default=3, ge=1, le=6),
    min_combined_odds: float = Query(default=1.6, ge=1.0, le=50.0),
    coupons_per_day: int = Query(
        default=3,
        ge=1,
        le=10,
        description="How many non-overlapping coupons to construct per match-day. Each composed coupon is evaluated leg-by-leg.",
    ),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Hit-rate of picks our coupon strategy would actually select.

    This is the number the user cares about: "if I had followed your coupons,
    how often would each leg land?" Unlike /accuracy, it ignores 0.5 Üst and
    other markets that are mathematically near-certain but never coupon-eligible.
    """
    stmt = (
        select(Match, Prediction)
        .join(Prediction, Prediction.match_id == Match.id)
        .options(joinedload(Match.home_team), joinedload(Match.away_team))
        .where(Match.ft_home.is_not(None))
        .where(Match.ft_away.is_not(None))
        .order_by(Match.kickoff.desc())
        .limit(limit)
    )
    if league:
        stmt = stmt.where(Match.league == league)

    rows = db.execute(stmt).all()

    seen: set[int] = set()
    match_ctx: list[tuple[Match, Prediction]] = []
    for m, p in rows:
        if m.id in seen:
            continue
        seen.add(m.id)
        match_ctx.append((m, p))

    # Batched odds lookup, preferring closing sources.
    source_priority = {"B365C": 0, "PSC": 1, "B365": 2, "PS": 3, "WH": 4}
    odds_rows = (
        db.query(Odds).filter(Odds.match_id.in_(list(seen))).all() if seen else []
    )
    odds_by_match: dict[int, dict[tuple[str, str], float]] = {}
    seen_prio: dict[int, dict[tuple[str, str], int]] = {}
    for o in odds_rows:
        keys = [(o.market, o.selection)]
        if o.market.startswith("OU_"):
            keys.append((f"over_under_{o.market[3:]}", o.selection))
        for key in keys:
            prio = source_priority.get(o.source, 99)
            prev = seen_prio.setdefault(o.match_id, {}).get(key, 99)
            if prio <= prev:
                odds_by_match.setdefault(o.match_id, {})[key] = float(o.decimal_odds)
                seen_prio[o.match_id][key] = prio

    # Group finished matches by calendar date and run the real composer on
    # each day, mirroring how the live /coupons endpoint picks bets. Only
    # legs that actually make it into a composed coupon are counted — this
    # is the honest "strategy hit rate" number.
    from collections import defaultdict
    by_day: dict[str, list[tuple[Match, Prediction]]] = defaultdict(list)
    for m, pred in match_ctx:
        by_day[m.kickoff.date().isoformat()].append((m, pred))

    ft_by_match: dict[int, tuple[int, int]] = {
        m.id: (m.ft_home, m.ft_away) for m, _ in match_ctx
    }

    total_legs = 0
    total_hits = 0
    total_stake = 0.0
    total_return = 0.0
    coupons_total = 0
    coupons_winning = 0
    by_market: dict[str, dict[str, Any]] = {}
    bin_edges = [(0.55, 0.65), (0.65, 0.75), (0.75, 0.85), (0.85, 1.01)]
    calib: list[dict[str, Any]] = [
        {"range": f"{int(lo*100)}-{int(hi*100 if hi <= 1 else 100)}%",
         "picks": 0, "hits": 0, "prob_sum": 0.0}
        for lo, hi in bin_edges
    ]
    details: list[dict[str, Any]] = []

    for day, day_matches in sorted(by_day.items()):
        # Enumerate every candidate pick across this day's matches.
        day_picks = []
        for m, pred in day_matches:
            day_picks.extend(_enumerate_picks(
                match_id=m.id,
                home=m.home_team.name,
                away=m.away_team.name,
                kickoff=m.kickoff.isoformat(),
                league=m.league,
                payload=pred.payload,
                odds_by_key=odds_by_match.get(m.id, {}),
                allowed_markets=_STRATEGY_MARKETS,
                min_prob=min_prob,
            ))
        day_picks.sort(key=lambda p: p.composite, reverse=True)

        # Compose up to N coupons per day, excluding matches already used.
        excluded: set[int] = set()
        for _ in range(coupons_per_day):
            coupon = _compose_coupon(
                day_picks,
                num_legs=num_legs,
                min_combined_odds=min_combined_odds,
                enforce_market_diversity=True,
                excluded_match_ids=excluded,
            )
            if coupon is None or coupon.combined_odds < min_combined_odds:
                break
            coupons_total += 1
            coupon_hit = True
            coupon_return = 1.0
            for leg in coupon.legs:
                excluded.add(leg.match_id)
                ft = ft_by_match.get(leg.match_id)
                if ft is None:
                    continue
                ft_home, ft_away = ft
                hit = _did_pick_hit(leg.market, leg.selection, ft_home, ft_away)
                total_legs += 1
                total_hits += int(hit)
                coupon_return *= (leg.book_odds or (1 / max(leg.prob, 1e-6)))
                if not hit:
                    coupon_hit = False

                base = _base_market(leg.market)
                bm = by_market.setdefault(
                    base, {"picks": 0, "hits": 0, "prob_sum": 0.0, "composite_sum": 0.0}
                )
                bm["picks"] += 1
                bm["hits"] += int(hit)
                bm["prob_sum"] += leg.prob
                bm["composite_sum"] += leg.composite

                for i, (lo, hi) in enumerate(bin_edges):
                    if lo <= leg.prob < hi:
                        calib[i]["picks"] += 1
                        calib[i]["hits"] += int(hit)
                        calib[i]["prob_sum"] += leg.prob
                        break

                if len(details) < 200:
                    details.append({
                        "match_id": leg.match_id,
                        "kickoff": leg.kickoff,
                        "league": leg.league,
                        "home_team": leg.home_team,
                        "away_team": leg.away_team,
                        "score": f"{ft_home}-{ft_away}",
                        "market": leg.market,
                        "market_label": leg.market_label,
                        "selection": leg.selection,
                        "selection_label": leg.selection_label,
                        "prob": leg.prob,
                        "book_odds": leg.book_odds,
                        "composite": leg.composite,
                        "reasons": leg.reasons,
                        "hit": hit,
                    })

            # Coupon-level P/L: 1u stake per coupon; all legs must hit to return.
            total_stake += 1.0
            if coupon_hit:
                coupons_winning += 1
                total_return += coupon_return

    for b in by_market.values():
        n = b["picks"]
        b["hit_rate"] = b["hits"] / n if n else 0.0
        b["avg_prob"] = b["prob_sum"] / n if n else 0.0
        b["avg_composite"] = b["composite_sum"] / n if n else 0.0
        del b["prob_sum"]
        del b["composite_sum"]

    for c in calib:
        n = c["picks"]
        c["hit_rate"] = c["hits"] / n if n else 0.0
        c["avg_prob"] = c["prob_sum"] / n if n else 0.0
        del c["prob_sum"]

    details.sort(key=lambda d: d["kickoff"], reverse=True)

    return {
        "overall": {
            "coupons": coupons_total,
            "coupons_won": coupons_winning,
            "coupon_win_rate": (
                coupons_winning / coupons_total if coupons_total else 0.0
            ),
            "legs": total_legs,
            "legs_hit": total_hits,
            "leg_hit_rate": total_hits / total_legs if total_legs else 0.0,
            # Flat 1u-per-coupon P/L; a coupon pays combined odds when all legs hit.
            "roi": (total_return - total_stake) / total_stake if total_stake else 0.0,
            "matches_evaluated": len(match_ctx),
        },
        "by_market": by_market,
        "calibration": calib,
        "filters": {
            "min_prob": min_prob,
            "num_legs": num_legs,
            "min_combined_odds": min_combined_odds,
            "coupons_per_day": coupons_per_day,
            "allowed_markets": sorted(_STRATEGY_MARKETS),
            "league": league,
            "limit": limit,
        },
        "details": details,
    }

    # Spec-compatible aliases so UIs using the documented field names work:
    summary["markets"] = summary.get("by_market", {})
    compat_cal = []
    for c in summary.get("calibration", []):
        compat_cal.append({
            **c,
            "bin": c.get("range"),
            "expected": c.get("avg_prob"),
            "actual": c.get("hit_rate"),
            "n": c.get("picks"),
        })
    if compat_cal:
        summary["calibration"] = compat_cal
    return summary
