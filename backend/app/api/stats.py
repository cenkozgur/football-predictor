"""Accuracy / hit-rate tracking for past predictions."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.db import get_db
from app.ml.accuracy import evaluate_predictions, summarize
from app.models.match import Match
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
