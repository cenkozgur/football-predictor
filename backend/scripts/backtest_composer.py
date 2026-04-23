"""Walk-forward backtest of the full 6-signal composer on historical matches.

Why this exists
---------------
`backtest.py` measures the Dixon-Coles baseline alone — is the *probability
estimate* calibrated. But the user doesn't bet on a probability; they bet on
what the **composer** picks after weighing prob + value + motivation + form +
availability. This script asks the real question: **"if we had run today's
composer on the matches it could see, what ROI would it have produced?"**

Design
------
Walk-forward per league:
    1. Load every finished match sorted by kickoff.
    2. Refit Dixon-Coles every `refit_every` matches on *strictly prior* ones.
    3. For each match:
         a. Build `payload` (1X2 / DC / OU / BTTS) via `build_full_payload`
            — same function predict_upcoming.py uses.
         b. Compute form/motivation as of the match's kickoff — these are
            DB-query functions that already accept `asof`, so no leakage.
         c. Fetch closing odds for the match from the Odds table.
         d. Attach availability = None (api-football didn't exist pre-2024,
            so we can't backfill it honestly for older matches).
         e. Call `suggest_coupons` with today's composer defaults.
         f. Compare primary + alternatives to actual result → PnL.

Honest limitations this script accepts
-------------------------------------
- **Availability is zero-signal** for every backtest match. We can't pretend
  it was there. Modern composer weighs it 10%, so backtest ROI slightly
  understates live performance if availability truly helps.
- **xG is available only where our DB has it** (Big-5 in recent seasons). DC
  uses it automatically.
- **Odds snapshots are closing odds** (football-data.co.uk / api-football).
  If in live we get pre-closing odds, our real edge may differ.

These caveats are listed explicitly in the output so we don't over-trust a
number that has blind spots.

Usage
-----
    python scripts/backtest_composer.py --leagues E0,D1,I1,SP1,F1 --months 12
    python scripts/backtest_composer.py --all-leagues --months 24
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db import SessionLocal
from app.features.adjust import adjust_rates, score_matrix_from_rates
from app.features.form import compute_team_form
from app.features.motivation import compute_team_motivation
from app.features.standings import build_standings
from app.ml.coupons import suggest_coupons
from app.ml.dixon_coles import DixonColesModel
from app.ml.markets import build_full_payload
from app.models.match import Match
from app.models.odds import Odds
from app.models.team import Team


_ALLOWED_MARKETS = {"1X2", "double_chance", "over_under", "btts"}
_SOURCE_PRIORITY = {"B365C": 0, "PSC": 1, "B365": 2, "PS": 3, "WH": 4}


@dataclass
class SimulatedCoupon:
    kickoff: datetime
    kind: str  # primary | alternative | fallback
    legs: list[dict[str, Any]]
    combined_odds: float
    all_hit: bool  # did every leg hit?

    @property
    def pnl(self) -> float:
        return (self.combined_odds - 1.0) if self.all_hit else -1.0


@dataclass
class BacktestStats:
    coupons: list[SimulatedCoupon] = field(default_factory=list)
    # Per-market leg tracking (picks-level, not coupon-level)
    leg_by_market_picks: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    leg_by_market_hits: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    # Per-month ROI trend
    monthly_pnl: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    monthly_count: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def add_coupon(self, c: SimulatedCoupon) -> None:
        self.coupons.append(c)
        month = c.kickoff.strftime("%Y-%m")
        self.monthly_pnl[month] += c.pnl
        self.monthly_count[month] += 1
        for leg in c.legs:
            m = leg["market"].split("_")[0] if "_" in leg["market"] else leg["market"]
            # Collapse 'over_under_2.5' → 'over_under' etc.
            if leg["market"].startswith("over_under"):
                m = "over_under"
            elif leg["market"].startswith("OU_"):
                m = "over_under"
            self.leg_by_market_picks[m] += 1
            if leg.get("hit"):
                self.leg_by_market_hits[m] += 1


# ---- per-match helpers ---------------------------------------------------


def _did_leg_hit(leg: dict[str, Any], match: Match) -> bool | None:
    """Same settlement logic as resolve_coupons.py, inlined to avoid import
    cycle and keep this script self-contained.

    Returns None if the match isn't resolved — backtest shouldn't hit that,
    but defensive.
    """
    h = match.ft_home
    a = match.ft_away
    if h is None or a is None:
        return None
    total = h + a
    market = leg["market"]
    sel = leg["selection"]

    if market == "1X2":
        if sel == "1":
            return h > a
        if sel == "X":
            return h == a
        if sel == "2":
            return a > h
    elif market == "double_chance":
        if sel == "1X":
            return h >= a
        if sel == "X2":
            return a >= h
        if sel == "12":
            return h != a
    elif market == "btts":
        both = h > 0 and a > 0
        return both if sel == "yes" else not both
    elif market.startswith("over_under_") or market.startswith("OU_"):
        line_str = market.split("_")[-1]
        try:
            line = float(line_str)
        except ValueError:
            return None
        if sel == "over":
            return total > line
        return total < line
    return None


def _odds_by_key_for_match(
    db, match_id: int
) -> dict[tuple[str, str], float]:
    """Closing-odds preferred index for a single match — same ordering as
    /coupons route."""
    rows = db.query(Odds).filter(Odds.match_id == match_id).all()
    by_key: dict[tuple[str, str], float] = {}
    seen_prio: dict[tuple[str, str], int] = {}
    for o in rows:
        keys: list[tuple[str, str]] = [(o.market, o.selection)]
        if o.market.startswith("OU_"):
            keys.append((f"over_under_{o.market[3:]}", o.selection))
        for key in keys:
            prio = _SOURCE_PRIORITY.get(o.source, 99)
            prev = seen_prio.get(key, 99)
            if prio <= prev:
                by_key[key] = float(o.decimal_odds)
                seen_prio[key] = prio
    return by_key


def _load_matches(db, leagues: list[str] | None, months: int) -> list[Match]:
    """Finished matches within window, ordered by kickoff."""
    cutoff = datetime.now(tz=timezone.utc).replace(tzinfo=None) - timedelta(days=30 * months)
    stmt = (
        select(Match)
        .where(Match.status == "finished")
        .where(Match.kickoff >= cutoff)
        .options(selectinload(Match.home_team), selectinload(Match.away_team))
        .order_by(Match.kickoff.asc())
    )
    if leagues:
        stmt = stmt.where(Match.league.in_(leagues))
    return list(db.scalars(stmt).all())


def _build_training_df(prior_matches: list[Match]) -> pd.DataFrame:
    """Same shape as predict_upcoming's training frame, restricted to
    matches strictly before the walk-forward boundary."""
    rows = []
    for m in prior_matches:
        if m.ft_home is None or m.ft_away is None:
            continue
        # Prefer xG if present (Dixon-Coles handles float goals in xG mode).
        xh = m.xg_home if m.xg_home is not None else float(m.ft_home)
        xa = m.xg_away if m.xg_away is not None else float(m.ft_away)
        rows.append({
            "league": m.league,
            "season": m.season,
            "home": m.home_team.name,
            "away": m.away_team.name,
            "home_goals": xh,
            "away_goals": xa,
            "kickoff": m.kickoff,
        })
    return pd.DataFrame(rows)


# ---- walk-forward loop ---------------------------------------------------


def run(
    leagues: list[str] | None,
    months: int,
    refit_every: int,
    min_train: int,
) -> None:
    print(
        f"Backtesting composer over last {months} months; "
        f"refit every {refit_every}, min train {min_train}.\n"
    )

    with SessionLocal() as db:
        matches = _load_matches(db, leagues, months)
        if not matches:
            print("No matches in window — nothing to backtest.")
            return

        # Training pool: every finished match older than the window start,
        # because those should be in the fit even though we're not testing
        # them. We include them so early-window matches have a meaningful
        # fit to walk forward from.
        window_start = matches[0].kickoff
        older_stmt = (
            select(Match)
            .where(Match.status == "finished")
            .where(Match.kickoff < window_start)
            .options(selectinload(Match.home_team), selectinload(Match.away_team))
            .order_by(Match.kickoff.asc())
        )
        if leagues:
            older_stmt = older_stmt.where(Match.league.in_(leagues))
        older = list(db.scalars(older_stmt).all())

        print(
            f"Training pool: {len(older)} matches before window + "
            f"{len(matches)} matches in test window."
        )
        if len(older) + min_train > len(older) + len(matches):
            # Nothing to test — not enough data.
            print("Not enough training data for walk-forward.")
            return

        stats = BacktestStats()
        # Cache standings per (league, season, kickoff-day) — same approach
        # as predict_upcoming for its in-round reuse.
        standings_cache: dict[tuple, Any] = {}

        def _cached_standings(league, season, asof):
            key = (league, season, asof.date())
            cached = standings_cache.get(key)
            if cached is None:
                cached = build_standings(db, league, season, asof)
                standings_cache[key] = cached
            return cached

        model: DixonColesModel | None = None
        matches_since_fit = refit_every  # force first fit

        for idx, m in enumerate(matches):
            # Walk-forward refit: on every `refit_every` match, retrain on
            # older + first `idx` matches. Keeps runtime reasonable.
            if matches_since_fit >= refit_every:
                train_df = _build_training_df(older + matches[:idx])
                if len(train_df) < min_train:
                    matches_since_fit += 1
                    continue
                try:
                    model = DixonColesModel.fit(train_df, xi=0.0018, l2=2.0)
                except Exception as exc:
                    print(f"  fit failed at idx {idx}: {exc}")
                    matches_since_fit += 1
                    continue
                matches_since_fit = 0
            else:
                matches_since_fit += 1

            if model is None:
                continue

            home_name = m.home_team.name
            away_name = m.away_team.name
            if home_name not in model.attack or away_name not in model.attack:
                continue

            base_lam, base_mu = model.rates(home_name, away_name)

            # Motivation: standings as of kickoff
            try:
                standings = _cached_standings(m.league, m.season, m.kickoff)
                home_mot = compute_team_motivation(standings, m.home_team_id)
                away_mot = compute_team_motivation(standings, m.away_team_id)
                adj = adjust_rates(base_lam, base_mu, home_mot, away_mot)
            except Exception:
                adj = type("A", (), {
                    "lam": base_lam, "mu": base_mu,
                    "base_lambda": base_lam, "base_mu": base_mu,
                    "home_multiplier": 1.0, "away_multiplier": 1.0,
                    "reasons": [],
                })()
                home_mot = away_mot = None

            # Form: same asof semantics
            try:
                home_form = compute_team_form(
                    db, m.home_team_id, home_name, m.league, m.season, m.kickoff
                )
                away_form = compute_team_form(
                    db, m.away_team_id, away_name, m.league, m.season, m.kickoff
                )
            except Exception:
                home_form = away_form = None

            matrix = score_matrix_from_rates(adj.lam, adj.mu, model.rho)
            payload = build_full_payload(matrix)
            payload["context"] = {
                "home_motivation": _mot_to_dict(home_mot),
                "away_motivation": _mot_to_dict(away_mot),
                "home_form": _form_to_dict(home_form),
                "away_form": _form_to_dict(away_form),
                # Availability is None for backtest (see module docstring).
                "home_availability": None,
                "away_availability": None,
            }

            odds_by_key = _odds_by_key_for_match(db, m.id)

            result = suggest_coupons(
                [{
                    "match_id": m.id,
                    "home_team": home_name,
                    "away_team": away_name,
                    "kickoff": m.kickoff.isoformat(),
                    "league": m.league,
                    "payload": payload,
                }],
                min_prob_per_leg=0.55,
                num_legs=1,  # single-leg coupons per match in backtest
                min_legs=1,
                max_legs=1,
                allowed_markets=_ALLOWED_MARKETS,
                odds_by_match={m.id: odds_by_key},
                min_combined_odds=1.3,
                enforce_market_diversity=False,
                max_coupons=1,
            )

            primary = result.get("primary")
            if not primary:
                continue
            legs = primary.get("legs") or []
            if not legs:
                continue
            # Settle: check if every leg hit
            settled: list[dict[str, Any]] = []
            all_hit = True
            for leg in legs:
                hit = _did_leg_hit(leg, m)
                leg["hit"] = hit
                if not hit:
                    all_hit = False
                settled.append(leg)
            combined_odds = float(primary.get("combined_odds") or 1.0)
            kind = "fallback" if primary.get("is_fallback") else "primary"
            stats.add_coupon(
                SimulatedCoupon(
                    kickoff=m.kickoff,
                    kind=kind,
                    legs=settled,
                    combined_odds=combined_odds,
                    all_hit=all_hit,
                )
            )

        _print_report(stats)


def _mot_to_dict(m) -> dict[str, Any] | None:
    if m is None:
        return None
    return {
        "team": getattr(m, "team_name", "") or "",
        "relegation_risk": getattr(m, "relegation_risk", 0),
        "title_push": getattr(m, "title_push", 0),
        "europe_push": getattr(m, "europe_push", 0),
        "dead_rubber": getattr(m, "dead_rubber", 0),
        "intensity": getattr(m, "intensity", 0),
    }


def _form_to_dict(f) -> dict[str, Any] | None:
    if f is None:
        return None
    return {
        "team": f.team_name,
        "form_delta": round(f.form_delta, 3),
        "reasons": f.reasons,
    }


def _print_report(stats: BacktestStats) -> None:
    n = len(stats.coupons)
    if n == 0:
        print("\nNo coupons generated during walk-forward — filters rejected every match.")
        return

    settled = [c for c in stats.coupons if c.all_hit is not None]
    won = [c for c in stats.coupons if c.all_hit]
    total_pnl = sum(c.pnl for c in stats.coupons)
    roi = total_pnl / n
    primary_count = sum(1 for c in stats.coupons if c.kind == "primary")
    fallback_count = sum(1 for c in stats.coupons if c.kind == "fallback")

    print("\n" + "=" * 60)
    print("COMPOSER BACKTEST RESULTS")
    print("=" * 60)
    print(f"Total coupons:     {n}")
    print(f"  primary:         {primary_count}")
    print(f"  fallback:        {fallback_count}")
    print(f"Won:               {len(won)} ({len(won)/n:.1%})")
    print(f"Total PnL:         {total_pnl:+.2f} units")
    print(f"ROI per coupon:    {roi:+.1%}")
    print()
    print("Per-market leg performance:")
    for market in sorted(stats.leg_by_market_picks.keys()):
        picks = stats.leg_by_market_picks[market]
        hits = stats.leg_by_market_hits[market]
        rate = hits / picks if picks else 0.0
        print(f"  {market:20} picks={picks:5d} hits={hits:5d} hit_rate={rate:.1%}")
    print()
    print("Monthly PnL trend:")
    for month in sorted(stats.monthly_pnl.keys()):
        pnl = stats.monthly_pnl[month]
        cnt = stats.monthly_count[month]
        monthly_roi = pnl / cnt if cnt else 0.0
        print(f"  {month}  coupons={cnt:4d}  PnL={pnl:+7.2f}  ROI={monthly_roi:+6.1%}")
    print()
    print("Caveats: availability signal is zero for every backtest coupon")
    print("(api-football didn't exist pre-2024). Live composer weighs it 10%.")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--leagues", default="E0,D1,I1,SP1,F1",
                   help="Comma-separated internal codes. Default: Big-5.")
    p.add_argument("--months", type=int, default=12,
                   help="Window (months back) to backtest over.")
    p.add_argument("--refit-every", type=int, default=20)
    p.add_argument("--min-train", type=int, default=200)
    args = p.parse_args()
    leagues = args.leagues.split(",") if args.leagues else None
    run(
        leagues=leagues,
        months=args.months,
        refit_every=args.refit_every,
        min_train=args.min_train,
    )


if __name__ == "__main__":
    main()
