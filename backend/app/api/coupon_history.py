"""Coupon history endpoints: past coupons + leg-by-leg resolution for the UI.

The live `/coupons` route is stateless; this module serves the persisted
snapshots so the app can show "geçmiş kuponlar" with hit/miss badges and
a running track record.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.models.coupon import Coupon, CouponLeg
from app.models.match import Match

router = APIRouter()


def _leg_payload(leg: CouponLeg, match: Match | None) -> dict[str, Any]:
    if match is not None:
        score = (
            f"{match.ft_home}-{match.ft_away}"
            if match.ft_home is not None and match.ft_away is not None
            else None
        )
        home_name = match.home_team.name
        away_name = match.away_team.name
        kickoff = match.kickoff.isoformat()
        status = match.status
    else:
        score = None
        home_name = away_name = "?"
        kickoff = None
        status = "unknown"
    return {
        "match_id": leg.match_id,
        "home_team": home_name,
        "away_team": away_name,
        "kickoff": kickoff,
        "match_status": status,
        "score": score,
        "market": leg.market,
        "selection": leg.selection,
        "selection_label": leg.selection_label,
        "prob": leg.prob,
        "book_odds": leg.book_odds,
        "value_edge": leg.value_edge,
        "hit": leg.hit,
    }


def _coupon_payload(c: Coupon, match_index: dict[int, Match]) -> dict[str, Any]:
    return {
        "id": c.id,
        "generated_on": c.generated_on,
        "generated_at": c.generated_at.isoformat() if c.generated_at else None,
        "kind": c.kind,
        "num_legs": c.num_legs,
        "combined_prob": c.combined_prob,
        "combined_odds": c.combined_odds,
        "avg_composite": c.avg_composite,
        "hit": c.hit,
        "resolved_at": c.resolved_at.isoformat() if c.resolved_at else None,
        "legs": [_leg_payload(leg, match_index.get(leg.match_id)) for leg in c.legs],
    }


@router.get("/history")
def coupon_history(
    days: int = Query(default=30, ge=1, le=180),
    status: str | None = Query(
        default=None,
        description="Filter by resolution: 'pending', 'won', 'lost', or omit for all.",
    ),
    kind: str | None = Query(
        default=None,
        description="Filter by 'primary' or 'alternative'.",
    ),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Past coupons with per-leg resolution, newest-first."""
    since = (date.today() - timedelta(days=days)).isoformat()
    stmt = (
        select(Coupon)
        .where(Coupon.generated_on >= since)
        .options(selectinload(Coupon.legs))
        .order_by(Coupon.generated_at.desc())
    )
    if kind:
        stmt = stmt.where(Coupon.kind == kind)

    coupons = list(db.scalars(stmt).all())

    if status == "pending":
        coupons = [c for c in coupons if c.hit is None]
    elif status == "won":
        coupons = [c for c in coupons if c.hit is True]
    elif status == "lost":
        coupons = [c for c in coupons if c.hit is False]

    # Single round-trip to get every referenced match
    match_ids = {leg.match_id for c in coupons for leg in c.legs}
    match_rows = (
        db.scalars(
            select(Match)
            .where(Match.id.in_(match_ids))
            .options(selectinload(Match.home_team), selectinload(Match.away_team))
        ).all()
        if match_ids
        else []
    )
    match_index = {m.id: m for m in match_rows}

    total = len(coupons)
    settled = [c for c in coupons if c.hit is not None]
    won = [c for c in settled if c.hit]
    # ROI treats each coupon as a 1-unit stake: win → (odds-1), lose → -1.
    roi_numer = 0.0
    roi_denom = 0
    for c in settled:
        if c.combined_odds is None:
            continue
        roi_denom += 1
        roi_numer += (c.combined_odds - 1.0) if c.hit else -1.0

    summary = {
        "total": total,
        "settled": len(settled),
        "pending": total - len(settled),
        "won": len(won),
        "lost": len(settled) - len(won),
        "win_rate": (len(won) / len(settled)) if settled else None,
        "roi": (roi_numer / roi_denom) if roi_denom else None,
        "days": days,
    }

    return {
        "summary": summary,
        "coupons": [_coupon_payload(c, match_index) for c in coupons],
    }
