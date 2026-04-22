"""Generate predictions for upcoming (or arbitrary) matches and store them.

This is the production bridge between the research backtest and the app:
the backtest proved that pooled Dixon-Coles with L2=2 is the best single-model
configuration we have, and this script puts those predictions into the DB so
the FastAPI `/predictions/{match_id}` endpoint can return them to a UI.

Model
-----
    - Pooled Dixon-Coles across every ingested league (per-league gamma+delta,
      shared alpha/beta/rho)
    - L2 = 2.0 ridge penalty on team strengths (structural calibration fix
      from the research phase)
    - xi = 0.0018 time-decay weight on training matches
    - Fit on every finished match in the DB at invocation time. The fit is
      fast (~30s on 12k matches) so we refit on every run — no checkpointing.

Outputs
-------
For each target match we compute the joint score matrix, derive the full
multi-market payload via `app.ml.markets.build_full_payload`, and upsert a
row into the `predictions` table keyed on match_id (latest run wins).

Usage
-----
    # Predict every upcoming fixture (future kickoff, not finished)
    python scripts/predict_upcoming.py

    # Restrict to specific leagues
    python scripts/predict_upcoming.py --leagues E0,D1

    # Predict specific match IDs (any status)
    python scripts/predict_upcoming.py --match-ids 1,2,3

    # Demo mode: predict the N most recent finished matches (useful until
    # an upcoming-fixtures ingester exists — lets you see real predictions
    # through the API and UI immediately, on matches whose outcomes we also
    # happen to know so you can eyeball correctness)
    python scripts/predict_upcoming.py --recent 20
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from app.db import SessionLocal
from app.features.adjust import adjust_rates, score_matrix_from_rates
from app.features.form import compute_team_form
from app.features.motivation import compute_team_motivation
from app.features.standings import build_standings
from app.ml.dixon_coles import DixonColesModel
from app.ml.markets import build_full_payload
from app.models.match import Match
from app.models.prediction import Prediction


MODEL_L2 = 2.0
MODEL_XI = 0.0018


def _build_model_version(n_train: int, n_xg: int = 0) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    tag = f"xg{n_xg}" if n_xg > 0 else "g"
    return f"dc-pooled-l2_{MODEL_L2}-xi_{MODEL_XI}-{tag}-mot-n_{n_train}-{stamp}"


_STANDINGS_CACHE: dict[tuple[str, str, str], object] = {}


def _standings_cached(db, league: str, season: str, kickoff: datetime):
    """Avoid rebuilding the same table for every match in the same round."""
    # Bucket by day so close-together fixtures in a round share a cache entry.
    day_key = kickoff.strftime("%Y-%m-%d") if kickoff else "now"
    key = (league, season, day_key)
    if key not in _STANDINGS_CACHE:
        _STANDINGS_CACHE[key] = build_standings(db, league, season, kickoff)
    return _STANDINGS_CACHE[key]


def _mot_to_json(m):
    if m is None:
        return None
    return {
        "team": m.team_name,
        "rank": m.rank,
        "played": m.played,
        "matches_left": m.matches_left,
        "relegation_risk": round(m.relegation_risk, 3),
        "title_push": round(m.title_push, 3),
        "europe_push": round(m.europe_push, 3),
        "dead_rubber": round(m.dead_rubber, 3),
        "intensity": round(m.intensity, 3),
        "reasons": m.reasons,
    }


def _form_to_json(f):
    if f is None:
        return None
    return {
        "team": f.team_name,
        "games": f.games,
        "wins": f.wins,
        "draws": f.draws,
        "losses": f.losses,
        "goals_for": f.goals_for,
        "goals_against": f.goals_against,
        "win_rate": round(f.win_rate, 3),
        "clean_sheet_rate": round(f.clean_sheet_rate, 3),
        "fail_to_score_rate": round(f.fail_to_score_rate, 3),
        "gf_per_game": round(f.gf_per_game, 2),
        "ga_per_game": round(f.ga_per_game, 2),
        "home_gf_per_game": round(f.home_gf_per_game, 2),
        "home_ga_per_game": round(f.home_ga_per_game, 2),
        "away_gf_per_game": round(f.away_gf_per_game, 2),
        "away_ga_per_game": round(f.away_ga_per_game, 2),
        "form_delta": round(f.form_delta, 3),
        "reasons": f.reasons,
    }


def _availability_to_json(row):
    """TeamAvailability → the shape the composer reads off payload.context."""
    if row is None:
        return None
    return {
        "absent_count": row.absent_count,
        "key_absent_count": row.key_absent_count,
        "key_absences": row.key_absences or [],
    }


def _load_availability(db, match_ids: list[int]) -> dict[tuple[int, int], object]:
    """Return {(match_id, team_id): TeamAvailability} for a batch of matches.

    One query instead of N; the composer-facing attach loop below just looks up
    home and away in the dict. Missing (match, team) pairs return None, which
    is indistinguishable from "no reported absences" at the composer level.
    """
    from app.models.team_availability import TeamAvailability
    from sqlalchemy import select

    if not match_ids:
        return {}
    rows = db.scalars(
        select(TeamAvailability).where(TeamAvailability.match_id.in_(match_ids))
    ).all()
    return {(r.match_id, r.team_id): r for r in rows}


def _days_ago(kickoff: datetime, now: datetime) -> int:
    """SQLite stores tz-aware datetimes as naive UTC, so normalize both sides."""
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=timezone.utc)
    return max(0, (now - kickoff).days)


def _load_training_frame(db) -> tuple[pd.DataFrame, int]:
    """Load every finished match as a Dixon-Coles training row.

    When Understat xG is available for a match we use it as the goal signal
    instead of realized goals — training the model on "deserved" expected
    goals is less noisy than on 1-sample Poisson realizations. Falls back to
    ft_home/ft_away when xG is missing (Championship, Eredivisie, Primeira
    and any pre-2014 historical match).
    """
    rows = db.scalars(
        select(Match)
        .where(Match.status == "finished")
        .options(selectinload(Match.home_team), selectinload(Match.away_team))
    ).all()

    today = datetime.now(tz=timezone.utc)
    records = []
    n_xg = 0
    for m in rows:
        if m.xg_home is not None and m.xg_away is not None:
            hg, ag = float(m.xg_home), float(m.xg_away)
            n_xg += 1
        else:
            hg, ag = float(m.ft_home), float(m.ft_away)
        records.append({
            "home_team": m.home_team.name,
            "away_team": m.away_team.name,
            "home_goals": hg,
            "away_goals": ag,
            "league": m.league,
            "days_ago": _days_ago(m.kickoff, today),
        })
    if records:
        print(f"  using xG for {n_xg}/{len(records)} training rows")
    return pd.DataFrame.from_records(records), n_xg


def _select_target_matches(
    db,
    leagues: list[str] | None,
    match_ids: list[int] | None,
    recent: int | None,
) -> list[Match]:
    """Pick the matches we will predict, in priority order:
    explicit match IDs → recent finished (demo) → all upcoming fixtures.
    """
    stmt = select(Match).options(
        selectinload(Match.home_team), selectinload(Match.away_team)
    )

    if match_ids:
        stmt = stmt.where(Match.id.in_(match_ids))
    elif recent:
        # Most recent finished matches — demo mode until upcoming ingest exists.
        stmt = (
            stmt.where(Match.status == "finished")
            .order_by(Match.kickoff.desc())
            .limit(recent)
        )
    else:
        # Real upcoming-fixture mode. SQLite returns naive datetimes; the filter
        # still works because SQLAlchemy binds the parameter as ISO text and the
        # stored values are UTC.
        stmt = stmt.where(
            Match.kickoff >= datetime.now(tz=timezone.utc).replace(tzinfo=None),
            Match.status != "finished",
        ).order_by(Match.kickoff.asc())

    if leagues:
        stmt = stmt.where(Match.league.in_(leagues))

    return list(db.scalars(stmt).all())


def run(
    leagues: list[str] | None = None,
    match_ids: list[int] | None = None,
    recent: int | None = None,
) -> int:
    """Fit the pooled model once, then predict every target match.

    Returns the number of Prediction rows written.
    """
    with SessionLocal() as db:
        df, n_xg = _load_training_frame(db)
        if df.empty:
            print("No finished matches in DB — run scripts/ingest_all.py first.")
            return 0

        print(
            f"Training on {len(df)} finished matches across "
            f"{df['league'].nunique()} leagues "
            f"(L2={MODEL_L2}, xi={MODEL_XI})..."
        )
        model = DixonColesModel.fit(df, xi=MODEL_XI, l2=MODEL_L2)
        print(
            f"Fit complete: {len(model.teams)} teams, rho={model.rho:+.3f}, "
            f"leagues={len(model.leagues)}"
        )

        targets = _select_target_matches(db, leagues, match_ids, recent)
        if not targets:
            print(
                "No target matches selected. Try --recent 10 to predict the "
                "last 10 finished matches for demo purposes."
            )
            return 0

        model_version = _build_model_version(len(df), n_xg=n_xg)
        written = 0
        skipped_unknown_team = 0

        # Batch-load availability for every target so the inner loop is just a
        # dict lookup — one query rather than 2×N (one per side per match).
        availability_by_key = _load_availability(db, [m.id for m in targets])

        for m in targets:
            home_name = m.home_team.name
            away_name = m.away_team.name
            if home_name not in model.attack or away_name not in model.attack:
                unknown = []
                if home_name not in model.attack:
                    unknown.append(f"home={home_name}")
                if away_name not in model.attack:
                    unknown.append(f"away={away_name}")
                print(f"  skip {m.league} {home_name} vs {away_name} — {', '.join(unknown)}")
                skipped_unknown_team += 1
                continue

            base_lam, base_mu = model.rates(home_name, away_name)

            # Motivation adjustment: build the league table as of kickoff
            # and derive per-team motivation. Same-league cache because all
            # matches in one fixture round share a standings snapshot.
            standings = _standings_cached(db, m.league, m.season, m.kickoff)
            home_mot = compute_team_motivation(standings, m.home_team_id)
            away_mot = compute_team_motivation(standings, m.away_team_id)
            adj = adjust_rates(base_lam, base_mu, home_mot, away_mot)

            home_form = compute_team_form(
                db, m.home_team_id, home_name, m.league, m.season, m.kickoff
            )
            away_form = compute_team_form(
                db, m.away_team_id, away_name, m.league, m.season, m.kickoff
            )

            matrix = score_matrix_from_rates(adj.lam, adj.mu, model.rho)
            payload = build_full_payload(matrix)

            home_avail = availability_by_key.get((m.id, m.home_team_id))
            away_avail = availability_by_key.get((m.id, m.away_team_id))

            # Attach context block so the UI can render the "neden" panel.
            payload["context"] = {
                "base_lambda": {"home": adj.base_lambda, "away": adj.base_mu},
                "adjusted_lambda": {"home": adj.lam, "away": adj.mu},
                "multipliers": {"home": adj.home_multiplier, "away": adj.away_multiplier},
                "home_motivation": _mot_to_json(home_mot),
                "away_motivation": _mot_to_json(away_mot),
                "home_form": _form_to_json(home_form),
                "away_form": _form_to_json(away_form),
                "home_availability": _availability_to_json(home_avail),
                "away_availability": _availability_to_json(away_avail),
                "reasons": adj.reasons,
            }

            # Upsert: drop any existing Prediction rows for this match and
            # insert one fresh row. The API route orders by created_at desc
            # and takes the newest anyway, but this keeps the table compact.
            db.execute(delete(Prediction).where(Prediction.match_id == m.id))
            db.add(
                Prediction(
                    match_id=m.id,
                    model_version=model_version,
                    payload=payload,
                    lambda_home=float(adj.lam),
                    lambda_away=float(adj.mu),
                )
            )
            written += 1

        db.commit()
        print(
            f"Wrote {written} predictions "
            f"({skipped_unknown_team} skipped for unknown teams) "
            f"under model_version={model_version}"
        )
        return written


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Predict upcoming fixtures with the pooled L2-regularized Dixon-Coles model."
    )
    parser.add_argument(
        "--leagues",
        default=None,
        help="Comma-separated league codes to restrict prediction targets (not training).",
    )
    parser.add_argument(
        "--match-ids",
        default=None,
        help="Comma-separated match IDs to predict (overrides --recent and upcoming default).",
    )
    parser.add_argument(
        "--recent",
        type=int,
        default=None,
        help="Demo mode: predict the N most recent finished matches.",
    )
    args = parser.parse_args()

    leagues = args.leagues.split(",") if args.leagues else None
    match_ids = [int(x) for x in args.match_ids.split(",")] if args.match_ids else None

    run(leagues=leagues, match_ids=match_ids, recent=args.recent)


if __name__ == "__main__":
    main()
