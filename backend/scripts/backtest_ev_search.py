"""EV+ strategy search: scan historical matches, enumerate every candidate
pick, grid-search thresholds to find a combination with positive expected
value.

Why a separate script
---------------------
`backtest_composer.py` tests today's production composer with its fixed
thresholds and returned 0 coupons — the 3% edge gate was too tight for
Big-5 1X2. This script asks the opposite question: **given our data, does
ANY edge threshold + market + league subset produce a positive EV?**

How it works
------------
1. Walk-forward per league, same refit cadence as before.
2. For each match, enumerate candidate picks across 1X2, Double Chance,
   Over/Under (every line), BTTS. For each pick store:
        - model_prob, book_odds, actual outcome
3. Walk-forward is over; we now have a flat table of ~10k-50k candidate
   picks with ground-truth outcomes.
4. For every (market, league_set, edge_threshold, min_prob) tuple, compute
   ROI on the picks that pass. Print the grid.

Output
------
A ranked table of strategies by ROI. If any cell shows consistently
positive ROI across time buckets, that's the strategy we ship.

Usage
-----
    python scripts/backtest_ev_search.py --months 18
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
from app.ml.dixon_coles import DixonColesModel
from app.ml.markets import build_full_payload
from app.models.match import Match
from app.models.odds import Odds


# Same source priority as composer
_SOURCE_PRIORITY = {"B365C": 0, "PSC": 1, "B365": 2, "PS": 3, "WH": 4}

# Which market groups we evaluate. Each group maps to the base_market string
# used in coupon payloads; 1X2 is kept separately for comparison.
_MARKET_GROUPS = {
    "1X2": ["1X2"],
    "DC": ["double_chance"],
    "OU": ["over_under"],
    "BTTS": ["btts"],
    "Goals": ["over_under", "btts"],  # OU + BTTS combined
    "All": ["1X2", "double_chance", "over_under", "btts"],
}

# League groups
_BIG5 = {"E0", "D1", "I1", "SP1", "F1"}
_SMALL = {"FIN", "POL", "IRL", "SC0", "ROU", "NOR", "SWE", "T1", "AUT", "B1", "DNK", "N1", "P1", "E1"}


@dataclass
class Candidate:
    league: str
    kickoff: datetime
    market: str          # 1X2 | double_chance | over_under_2.5 | btts etc.
    base_market: str     # 1X2 | double_chance | over_under | btts
    selection: str
    model_prob: float
    book_odds: float
    book_prob: float
    edge: float          # (model_prob / fair_book_prob) - 1
    hit: bool            # actual outcome

    @property
    def pnl(self) -> float:
        return (self.book_odds - 1.0) if self.hit else -1.0


def _base(market: str) -> str:
    if market.startswith("over_under"):
        return "over_under"
    if market.startswith("OU_"):
        return "over_under"
    return market


def _did_hit(market: str, selection: str, ft_home: int, ft_away: int) -> bool | None:
    if ft_home is None or ft_away is None:
        return None
    total = ft_home + ft_away
    if market == "1X2":
        if selection == "1": return ft_home > ft_away
        if selection == "X": return ft_home == ft_away
        if selection == "2": return ft_away > ft_home
    if market == "double_chance":
        if selection == "1X": return ft_home >= ft_away
        if selection == "X2": return ft_away >= ft_home
        if selection == "12": return ft_home != ft_away
    if market == "btts":
        both = ft_home > 0 and ft_away > 0
        return both if selection == "yes" else not both
    if market.startswith("over_under_"):
        try:
            line = float(market.split("_")[-1])
        except ValueError:
            return None
        if selection == "over": return total > line
        return total < line
    return None


def _odds_index(db, match_id: int) -> dict[tuple[str, str], float]:
    rows = db.query(Odds).filter(Odds.match_id == match_id).all()
    by_key: dict[tuple[str, str], float] = {}
    seen: dict[tuple[str, str], int] = {}
    for o in rows:
        keys = [(o.market, o.selection)]
        if o.market.startswith("OU_"):
            keys.append((f"over_under_{o.market[3:]}", o.selection))
        for key in keys:
            prio = _SOURCE_PRIORITY.get(o.source, 99)
            if prio <= seen.get(key, 99):
                by_key[key] = float(o.decimal_odds)
                seen[key] = prio
    return by_key


def _overround(odds_by_key: dict, market: str, selections: list[str]) -> float:
    total = 0.0
    for sel in selections:
        o = odds_by_key.get((market, sel))
        if o is None and market.startswith("over_under_"):
            o = odds_by_key.get((f"OU_{market[len('over_under_'):]}", sel))
        if o is None or o <= 0:
            return 1.0
        total += 1.0 / o
    return total if total > 0 else 1.0


def _enumerate_candidates(
    match: Match, payload: dict[str, Any], odds_by_key: dict[tuple[str, str], float]
) -> list[Candidate]:
    """One Candidate per (market, selection) that has both a prob and odds."""
    out: list[Candidate] = []

    def emit(market: str, selection: str, prob: float, overround: float):
        odds = odds_by_key.get((market, selection))
        if odds is None and market.startswith("over_under_"):
            odds = odds_by_key.get((f"OU_{market[len('over_under_'):]}", selection))
        if odds is None or odds <= 1.01:
            return
        book_prob = 1.0 / odds
        fair_prob = book_prob / overround
        edge = (prob / fair_prob) - 1.0
        hit = _did_hit(market, selection, match.ft_home, match.ft_away)
        if hit is None:
            return
        out.append(Candidate(
            league=match.league,
            kickoff=match.kickoff,
            market=market,
            base_market=_base(market),
            selection=selection,
            model_prob=prob,
            book_odds=odds,
            book_prob=book_prob,
            edge=edge,
            hit=hit,
        ))

    if "1X2" in payload:
        over = _overround(odds_by_key, "1X2", ["1", "X", "2"])
        for sel, p in payload["1X2"].items():
            emit("1X2", sel, float(p), over)
    if "double_chance" in payload:
        # DC overround across 1X,12,X2 lines — but we compare each DC selection
        # against the same vig, so use 1X2 overround as proxy
        over_dc = _overround(odds_by_key, "DC", ["1X", "12", "X2"])
        if over_dc == 1.0:
            over_dc = _overround(odds_by_key, "1X2", ["1", "X", "2"]) * 2 / 3 or 1.0
        for sel, p in payload["double_chance"].items():
            emit("double_chance", sel, float(p), over_dc)
    if "btts" in payload:
        over_btts = _overround(odds_by_key, "btts", ["yes", "no"])
        if over_btts == 1.0:
            over_btts = _overround(odds_by_key, "BTTS", ["yes", "no"])
        for sel, p in payload["btts"].items():
            emit("btts", sel, float(p), over_btts)
    if "over_under" in payload:
        for line, ou in payload["over_under"].items():
            market_prefix = f"over_under_{line}"
            over_ou = _overround(odds_by_key, market_prefix, ["over", "under"])
            for sel, p in ou.items():
                emit(market_prefix, sel, float(p), over_ou)

    return out


def _build_training_df(prior: list[Match], asof: datetime) -> pd.DataFrame:
    """DC expects columns: home_team, away_team, home_goals, away_goals,
    league, days_ago. Matches predict_upcoming._load_training_frame exactly."""
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


def walk_forward(
    leagues: list[str] | None, months: int, refit_every: int, min_train: int
) -> list[Candidate]:
    candidates: list[Candidate] = []
    with SessionLocal() as db:
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
        matches = list(db.scalars(stmt).all())
        if not matches:
            print("No matches in window.")
            return candidates

        older_stmt = (
            select(Match)
            .where(Match.status == "finished")
            .where(Match.kickoff < matches[0].kickoff)
            .options(selectinload(Match.home_team), selectinload(Match.away_team))
            .order_by(Match.kickoff.asc())
        )
        if leagues:
            older_stmt = older_stmt.where(Match.league.in_(leagues))
        older = list(db.scalars(older_stmt).all())

        print(
            f"Walk-forward: {len(older)} training matches before window, "
            f"{len(matches)} test matches."
        )

        standings_cache: dict[tuple, Any] = {}
        def _cstand(league, season, asof):
            k = (league, season, asof.date())
            c = standings_cache.get(k)
            if c is None:
                c = build_standings(db, league, season, asof)
                standings_cache[k] = c
            return c

        model: DixonColesModel | None = None
        since_fit = refit_every
        fits = 0

        for idx, m in enumerate(matches):
            if since_fit >= refit_every:
                train = _build_training_df(older + matches[:idx], asof=m.kickoff)
                if len(train) < min_train:
                    since_fit += 1
                    continue
                try:
                    model = DixonColesModel.fit(train, xi=0.0018, l2=2.0)
                    fits += 1
                except Exception as exc:
                    print(f"  fit failed idx={idx}: {exc}")
                    since_fit += 1
                    continue
                since_fit = 0
            else:
                since_fit += 1

            if model is None:
                continue

            hn = m.home_team.name
            an = m.away_team.name
            if hn not in model.attack or an not in model.attack:
                continue

            base_lam, base_mu = model.rates(hn, an)
            try:
                standings = _cstand(m.league, m.season, m.kickoff)
                home_mot = compute_team_motivation(standings, m.home_team_id)
                away_mot = compute_team_motivation(standings, m.away_team_id)
                adj = adjust_rates(base_lam, base_mu, home_mot, away_mot)
            except Exception:
                adj = type("A", (), {"lam": base_lam, "mu": base_mu, "rho": 0})()

            matrix = score_matrix_from_rates(adj.lam, adj.mu, model.rho)
            payload = build_full_payload(matrix)

            odds_by_key = _odds_index(db, m.id)
            if not odds_by_key:
                continue

            cands = _enumerate_candidates(m, payload, odds_by_key)
            candidates.extend(cands)

        print(f"Fits performed: {fits}, total candidates generated: {len(candidates)}")
    return candidates


# ---- grid search ---------------------------------------------------------


def grid_search(cands: list[Candidate], leagues: list[str] | None) -> None:
    if not cands:
        print("No candidates to evaluate.")
        return

    league_groups: dict[str, set[str]] = {"All": {c.league for c in cands}}
    big5 = _BIG5 & league_groups["All"]
    small = _SMALL & league_groups["All"]
    if big5: league_groups["Big5"] = big5
    if small: league_groups["Small"] = small

    edge_thresholds = [0.0, 0.01, 0.02, 0.03, 0.05, 0.08]
    min_probs = [0.50, 0.55, 0.60]

    print("\n" + "=" * 90)
    print(f"{'league':8} {'market':10} {'edge>=':7} {'min_p>=':8} {'picks':>7} {'hit%':>7} {'avg_odds':>9} {'ROI':>8}")
    print("=" * 90)

    results: list[tuple] = []
    for lg_name, lg_set in league_groups.items():
        for mk_name, mk_bases in _MARKET_GROUPS.items():
            for edge in edge_thresholds:
                for mp in min_probs:
                    subset = [
                        c for c in cands
                        if c.league in lg_set
                        and c.base_market in mk_bases
                        and c.edge >= edge
                        and c.model_prob >= mp
                    ]
                    n = len(subset)
                    if n < 50:
                        continue
                    hits = sum(1 for c in subset if c.hit)
                    pnl = sum(c.pnl for c in subset)
                    roi = pnl / n
                    avg_odds = sum(c.book_odds for c in subset) / n
                    hit_rate = hits / n
                    results.append((roi, n, lg_name, mk_name, edge, mp, hit_rate, avg_odds))

    # Sort by ROI descending, show top 30
    results.sort(reverse=True)
    for roi, n, lg, mk, edge, mp, hr, ao in results[:30]:
        print(f"{lg:8} {mk:10} {edge:6.2f}  {mp:7.2f}  {n:>7d}  {hr:6.1%}  {ao:>9.2f}  {roi:>+7.1%}")

    print("=" * 90)
    if results and results[0][0] > 0:
        best_roi, best_n, best_lg, best_mk, best_edge, best_mp, best_hr, best_ao = results[0]
        print(f"\n>>> BEST STRATEGY: {best_lg} / {best_mk} / edge>={best_edge:.2f} / prob>={best_mp:.2f}")
        print(f"    {best_n} picks, {best_hr:.1%} hit rate, avg odds {best_ao:.2f}, ROI {best_roi:+.1%}")
    else:
        print("\n>>> No positive-ROI strategy found in grid. The market is too efficient")
        print("    for our current signals, or our signals need strengthening.")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--leagues", default=None,
                   help="Comma-separated internal codes. Default: all with data.")
    p.add_argument("--months", type=int, default=18)
    p.add_argument("--refit-every", type=int, default=30)
    p.add_argument("--min-train", type=int, default=500)
    args = p.parse_args()
    leagues = args.leagues.split(",") if args.leagues else None
    cands = walk_forward(
        leagues=leagues,
        months=args.months,
        refit_every=args.refit_every,
        min_train=args.min_train,
    )
    grid_search(cands, leagues)


if __name__ == "__main__":
    main()
