"""Prediction API: public display (Tahmin) and authenticated value view (Değer).

Two related but distinct surfaces, split deliberately:

GET /predictions/{match_id}
    Public. Returns the full model payload plus match context so the Tahmin
    UI can render every market for every fixture. No user, no bankroll, no
    Kelly sizing. Matches the "show me what the model thinks" mental model
    the user has when browsing predictions.

GET /predictions/{match_id}/value
    Authenticated. Cross-references the stored prediction against the latest
    odds snapshot for the match, classifies each selection as banko / kombine
    / no value, computes a fractional Kelly stake against the user's bankroll,
    and returns only the rows the user has opted into via `mode`. This is the
    Değer surface — actionable value picks, never displayed without the user
    being logged in.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_current_user
from app.db import get_db
from app.ml.value import classify_selection, kelly_stake
from app.models.match import Match
from app.models.odds import Odds
from app.models.prediction import Prediction
from app.models.user import User

router = APIRouter()

Mode = Literal["all", "banko", "kombine"]


class MatchContext(BaseModel):
    id: int
    league: str
    season: str
    kickoff: datetime
    home_team: str
    away_team: str
    status: str


class PredictionView(BaseModel):
    """Public prediction payload for the Tahmin display surface."""

    match: MatchContext
    model_version: str
    generated_at: datetime
    lambda_home: float
    lambda_away: float
    payload: dict[str, Any]


class ValueSelection(BaseModel):
    market: str
    selection: str
    model_prob: float
    bilyoner_odds: float
    edge: float
    kelly_stake: float
    tag: str


class PredictionValueOut(BaseModel):
    """Authenticated value surface — Değer picks with Kelly sizing."""

    match_id: int
    model_version: str
    value: list[ValueSelection]


def _load_latest_prediction(db: Session, match_id: int) -> Prediction:
    pred = db.scalar(
        select(Prediction)
        .where(Prediction.match_id == match_id)
        .order_by(Prediction.created_at.desc())
    )
    if pred is None:
        raise HTTPException(status_code=404, detail="No prediction for this match")
    return pred


@router.get("/{match_id}", response_model=PredictionView)
def get_prediction(match_id: int, db: Session = Depends(get_db)) -> PredictionView:
    """Public Tahmin view: model payload + match context for display."""
    pred = _load_latest_prediction(db, match_id)
    match = db.scalar(
        select(Match)
        .where(Match.id == match_id)
        .options(selectinload(Match.home_team), selectinload(Match.away_team))
    )
    if match is None:
        raise HTTPException(status_code=404, detail="Match not found")
    return PredictionView(
        match=MatchContext(
            id=match.id,
            league=match.league,
            season=match.season,
            kickoff=match.kickoff,
            home_team=match.home_team.name,
            away_team=match.away_team.name,
            status=match.status,
        ),
        model_version=pred.model_version,
        generated_at=pred.created_at,
        lambda_home=pred.lambda_home,
        lambda_away=pred.lambda_away,
        payload=pred.payload,
    )


@router.get("/{match_id}/value", response_model=PredictionValueOut)
def get_prediction_value(
    match_id: int,
    mode: Mode = Query(default="all"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> PredictionValueOut:
    """Authenticated Değer view: value picks + Kelly stakes against the user's bankroll.

    mode=all      → every selection that qualifies as banko or kombine
    mode=banko    → only banko_value picks (high-confidence singles)
    mode=kombine  → only kombine_value picks (coupon candidates)
    """
    pred = _load_latest_prediction(db, match_id)

    odds_rows = db.scalars(
        select(Odds).where(Odds.match_id == match_id).order_by(Odds.fetched_at.desc())
    ).all()

    latest_odds: dict[tuple[str, str], float] = {}
    for o in odds_rows:
        key = (o.market, o.selection)
        if key not in latest_odds:
            latest_odds[key] = o.decimal_odds

    value_rows: list[ValueSelection] = []
    for market, selections in _iter_market_selections(pred.payload):
        for selection, prob in selections.items():
            odds = latest_odds.get((market, selection))
            if odds is None or prob <= 0:
                continue
            edge = prob * odds - 1.0
            tag = classify_selection(prob, edge)
            if tag == "no_value":
                continue
            if mode == "banko" and tag != "banko_value":
                continue
            if mode == "kombine" and tag != "kombine_value":
                continue
            value_rows.append(
                ValueSelection(
                    market=market,
                    selection=selection,
                    model_prob=prob,
                    bilyoner_odds=odds,
                    edge=edge,
                    kelly_stake=kelly_stake(
                        prob, odds, user.bankroll, user.kelly_fraction
                    ),
                    tag=tag,
                )
            )

    return PredictionValueOut(
        match_id=match_id,
        model_version=pred.model_version,
        value=value_rows,
    )


def _iter_market_selections(payload: dict[str, Any]):
    """Yield (market, {selection: prob}) pairs from a prediction payload.

    A market is any top-level dict whose values are all numeric. Nested
    structures (e.g. `over_under` contains dicts per line) and lists
    (e.g. `correct_score_top10`) are ignored by this simple shape check.
    """
    for key, value in payload.items():
        if isinstance(value, dict) and all(isinstance(v, (int, float)) for v in value.values()):
            yield key, value
