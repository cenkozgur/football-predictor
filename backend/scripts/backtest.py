"""Walk-forward backtest of the Dixon-Coles baseline across one or more leagues.

The single most important script in the project: without a walk-forward backtest
we have no way of knowing whether the model actually has an edge. Every future
model change (LightGBM, Bayesian, ensembles, calibration) is judged against this.

What it does
------------
For each league independently:
    1. Load every finished match from the DB, sorted by kickoff.
    2. Start at `min_train` (default 200). Before that we don't trust the fit.
    3. For match `i`, train Dixon-Coles on matches [0..i-1], then predict match i.
       To keep runtime sane, we only *refit* every `refit_every` matches
       (default 20) and reuse the last fit for intermediate matches.
       Each prediction still only sees strictly prior matches.
    4. Derive 1X2 and O/U 2.5 probabilities from the score matrix.
    5. Look up closing odds (B365C / PSC / B365) for the match. Simulate a 1-unit
       bet whenever model_prob * odds − 1 > threshold.
    6. Record each prediction and each simulated bet.

What it reports
---------------
- Brier score (lower is better; 0.20 is decent for 1X2, below 0.19 is good)
- Log loss
- Calibration table (predicted bucket vs actual hit rate)
- Per-market ROI at several edge thresholds
- Per-league breakdown + overall aggregate

Usage:
    python scripts/backtest.py                             # all leagues, default thresholds
    python scripts/backtest.py --leagues E0,D1
    python scripts/backtest.py --leagues E0 --min-train 150 --refit-every 10
    python scripts/backtest.py --xi 0.0018 --thresholds 0,0.03,0.05,0.10
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db import SessionLocal
from app.ingestion.leagues import BY_CODE, MAIN_LEAGUES, NEW_LEAGUES
from app.ml.calibration import TemperatureScaler
from app.ml.dixon_coles import DixonColesModel
from app.ml.markets import one_x_two, over_under
from app.models.match import Match  # noqa: TCH001  (used in helper type hints)
from app.models.odds import Odds


RESULT_CODE = {"1": "home_win", "X": "draw", "2": "away_win"}


@dataclass
class Bet:
    league: str
    kickoff: datetime
    market: str
    selection: str
    model_prob: float
    odds: float
    edge: float
    won: bool

    @property
    def pnl(self) -> float:
        return (self.odds - 1.0) if self.won else -1.0


@dataclass
class LeagueResult:
    league: str
    n_predictions: int = 0
    brier_sum: float = 0.0
    logloss_sum: float = 0.0
    bets: list[Bet] = field(default_factory=list)
    # For calibration: list of (predicted_prob, was_winner 0/1) across all 1X2 selections
    calibration_points: list[tuple[float, int]] = field(default_factory=list)
    # Latest fitted temperatures (1.0 if calibration disabled or never fit)
    final_T_1x2: float = 1.0
    final_T_ou25: float = 1.0
    n_calibrator_fits: int = 0
    # Correct-score quality: for each prediction, record the cumulative probability
    # mass the model assigned to its top-K most likely scorelines and whether the
    # actual result fell inside that top-K. If top1 hits at a higher rate than the
    # top1 probability predicts, the model has information the 1X2 summary discards.
    cs_top1_prob_sum: float = 0.0
    cs_top1_hits: int = 0
    cs_top3_prob_sum: float = 0.0
    cs_top3_hits: int = 0
    cs_top5_prob_sum: float = 0.0
    cs_top5_hits: int = 0

    @property
    def brier(self) -> float:
        return self.brier_sum / self.n_predictions if self.n_predictions else float("nan")

    @property
    def logloss(self) -> float:
        return self.logloss_sum / self.n_predictions if self.n_predictions else float("nan")


# ----------------------- data loading -----------------------


def load_league_matches(db: Session, league_code: str) -> list[Match]:
    return list(
        db.scalars(
            select(Match)
            .where(Match.league == league_code, Match.status == "finished")
            .order_by(Match.kickoff.asc())
            .options(selectinload(Match.home_team), selectinload(Match.away_team))
        )
    )


# Closing-odds priority: efficient-market reference. Honest but ~3-6 ROI points
# harder to beat than opening odds. Covers both main-format (B365C/PSC) and
# new-format aggregated closing sources (BFEC/AvgC/MaxC).
CLOSING_PRIORITY = [
    "B365C", "PSC", "BFEC", "B365", "PS", "WH",
    "AvgC", "MaxC", "Avg", "Max",
]
# Opening-odds priority: realistic for a bettor placing bets days before kickoff
# (e.g. on bilyoner.com). Main-format leagues have real opening sources; new-format
# leagues only have closing, so they fall through to the closing tail.
OPENING_PRIORITY = [
    "B365", "PS", "WH", "Avg", "Max",
    "B365C", "PSC", "BFEC", "AvgC", "MaxC",
]


def load_odds_index(
    db: Session, match_ids: list[int], priority: list[str]
) -> dict[int, dict[tuple[str, str], float]]:
    """Return {match_id: {(market, selection): odds}} respecting source priority.

    When multiple sources are present for the same (market, selection), the
    earliest entry in `priority` wins.
    """
    rank = {s: i for i, s in enumerate(priority)}

    rows = db.scalars(
        select(Odds).where(Odds.match_id.in_(match_ids))
    ).all()

    index: dict[int, dict[tuple[str, str], tuple[int, float]]] = defaultdict(dict)
    for o in rows:
        key = (o.market, o.selection)
        priority = rank.get(o.source, 99)
        current = index[o.match_id].get(key)
        if current is None or priority < current[0]:
            index[o.match_id][key] = (priority, o.decimal_odds)

    return {mid: {k: v[1] for k, v in d.items()} for mid, d in index.items()}


# ----------------------- prediction helper (shared by per-league and pooled) -----------------------


def _calibration_state() -> dict:
    """Mutable per-result calibration buffers + scalers, kept in a dict for ease."""
    return {
        "cal_1x2": TemperatureScaler(),
        "cal_ou25": TemperatureScaler(),
        "buf_1x2_probs": [],
        "buf_1x2_y": [],
        "buf_ou25_probs": [],
        "buf_ou25_y": [],
        "last_fit_index": -10**9,
    }


def _process_prediction(
    *,
    test_match: Match,
    test_league: str,
    model: DixonColesModel,
    odds_index: dict,
    result: LeagueResult,
    cal_state: dict,
    calibrate: bool,
    calib_min: int,
    calib_refit_every: int,
    calib_window: int,
    thresholds: list[float],
    global_index: int,
) -> None:
    """Score one match: predict, calibrate, record metrics, simulate bets, update calibrator.

    Hosts the logic that was duplicated between per-league and pooled walks.
    `test_league` is the league code to attribute the bet to (it must equal the
    test match's league in pooled mode; in per-league mode it's the same code
    everywhere). `global_index` is used by the calibrator's refit cadence.
    """
    home_name = test_match.home_team.name
    away_name = test_match.away_team.name
    if home_name not in model.attack or away_name not in model.attack:
        return  # unseen team

    matrix = model.score_matrix(home_name, away_name)
    raw_probs = one_x_two(matrix)
    raw_ou25 = over_under(matrix, 2.5)

    cal_1x2: TemperatureScaler = cal_state["cal_1x2"]
    cal_ou25: TemperatureScaler = cal_state["cal_ou25"]

    if calibrate and cal_1x2.fitted:
        cal = cal_1x2.apply(np.array([raw_probs["1"], raw_probs["X"], raw_probs["2"]]))
        probs = {"1": float(cal[0]), "X": float(cal[1]), "2": float(cal[2])}
    else:
        probs = raw_probs

    if calibrate and cal_ou25.fitted:
        cal = cal_ou25.apply(np.array([raw_ou25["over"], raw_ou25["under"]]))
        ou25 = {"over": float(cal[0]), "under": float(cal[1])}
    else:
        ou25 = raw_ou25

    if test_match.ft_home > test_match.ft_away:
        actual = {"1": 1, "X": 0, "2": 0}
        y_1x2 = 0
    elif test_match.ft_home < test_match.ft_away:
        actual = {"1": 0, "X": 0, "2": 1}
        y_1x2 = 2
    else:
        actual = {"1": 0, "X": 1, "2": 0}
        y_1x2 = 1
    y_ou25 = 0 if (test_match.ft_home + test_match.ft_away) > 2.5 else 1

    brier = sum((probs[k] - actual[k]) ** 2 for k in ("1", "X", "2"))
    p_actual = max(probs[[k for k, v in actual.items() if v == 1][0]], 1e-12)
    logloss = -float(np.log(p_actual))

    result.n_predictions += 1
    result.brier_sum += brier
    result.logloss_sum += logloss
    for k in ("1", "X", "2"):
        result.calibration_points.append((probs[k], actual[k]))

    # Correct-score quality: score_matrix already sums to 1 over all cells.
    # Take the top-K highest-probability cells and ask whether the actual result
    # landed in that set. We clip goals at matrix shape - 1 for the rare 8+ goal blowout.
    max_goals = matrix.shape[0] - 1
    flat = matrix.ravel()
    order = np.argsort(flat)[::-1]
    h_actual = min(int(test_match.ft_home), max_goals)
    a_actual = min(int(test_match.ft_away), max_goals)
    actual_flat_idx = h_actual * matrix.shape[1] + a_actual
    top1_idx = order[:1]
    top3_idx = order[:3]
    top5_idx = order[:5]
    result.cs_top1_prob_sum += float(flat[top1_idx].sum())
    result.cs_top1_hits += int(actual_flat_idx in top1_idx)
    result.cs_top3_prob_sum += float(flat[top3_idx].sum())
    result.cs_top3_hits += int(actual_flat_idx in top3_idx)
    result.cs_top5_prob_sum += float(flat[top5_idx].sum())
    result.cs_top5_hits += int(actual_flat_idx in top5_idx)

    match_odds = odds_index.get(test_match.id, {})

    for selection in ("1", "X", "2"):
        price = match_odds.get(("1X2", selection))
        if price is None:
            continue
        p = probs[selection]
        e = p * price - 1.0
        if e > min(thresholds):
            result.bets.append(
                Bet(
                    league=test_league,
                    kickoff=test_match.kickoff,
                    market="1X2",
                    selection=selection,
                    model_prob=p,
                    odds=price,
                    edge=e,
                    won=bool(actual[selection]),
                )
            )

    for selection in ("over", "under"):
        price = match_odds.get(("OU_2.5", selection))
        if price is None:
            continue
        p = ou25[selection]
        e = p * price - 1.0
        if e > min(thresholds):
            total = test_match.ft_home + test_match.ft_away
            won = (total > 2.5) if selection == "over" else (total < 2.5)
            result.bets.append(
                Bet(
                    league=test_league,
                    kickoff=test_match.kickoff,
                    market="OU_2.5",
                    selection=selection,
                    model_prob=p,
                    odds=price,
                    edge=e,
                    won=won,
                )
            )

    if calibrate:
        cal_state["buf_1x2_probs"].append([raw_probs["1"], raw_probs["X"], raw_probs["2"]])
        cal_state["buf_1x2_y"].append(y_1x2)
        cal_state["buf_ou25_probs"].append([raw_ou25["over"], raw_ou25["under"]])
        cal_state["buf_ou25_y"].append(y_ou25)
        if len(cal_state["buf_1x2_probs"]) > calib_window:
            cal_state["buf_1x2_probs"].pop(0)
            cal_state["buf_1x2_y"].pop(0)
            cal_state["buf_ou25_probs"].pop(0)
            cal_state["buf_ou25_y"].pop(0)
        if (
            len(cal_state["buf_1x2_probs"]) >= calib_min
            and global_index - cal_state["last_fit_index"] >= calib_refit_every
        ):
            cal_1x2.fit(np.array(cal_state["buf_1x2_probs"]), np.array(cal_state["buf_1x2_y"]))
            cal_ou25.fit(np.array(cal_state["buf_ou25_probs"]), np.array(cal_state["buf_ou25_y"]))
            cal_state["last_fit_index"] = global_index
            result.n_calibrator_fits += 1
            result.final_T_1x2 = cal_1x2.T
            result.final_T_ou25 = cal_ou25.T


# ----------------------- backtest core -----------------------


def backtest_league(
    db: Session,
    league_code: str,
    min_train: int,
    refit_every: int,
    xi: float,
    l2: float,
    thresholds: list[float],
    odds_priority: list[str],
    calibrate: bool,
    calib_min: int = 100,
    calib_refit_every: int = 50,
    calib_window: int = 500,
) -> LeagueResult:
    matches = load_league_matches(db, league_code)
    if len(matches) < min_train + 10:
        print(f"[{league_code}] only {len(matches)} matches, need at least {min_train + 10} — skipping")
        return LeagueResult(league=league_code)

    odds_index = load_odds_index(db, [m.id for m in matches], odds_priority)

    records = [
        {
            "kickoff": m.kickoff,
            "home_team": m.home_team.name,
            "away_team": m.away_team.name,
            "home_goals": m.ft_home,
            "away_goals": m.ft_away,
        }
        for m in matches
    ]
    df_all = pd.DataFrame.from_records(records)

    result = LeagueResult(league=league_code)
    model: DixonColesModel | None = None
    last_refit: int = -1
    cal_state = _calibration_state()

    print(
        f"[{league_code}] {len(matches)} matches — walk-forward from index {min_train} "
        f"(refit every {refit_every}; l2={l2}; calibrate={calibrate})"
    )

    for i in range(min_train, len(matches)):
        if model is None or i - last_refit >= refit_every:
            train_df = df_all.iloc[:i].copy()
            test_kickoff = matches[i].kickoff
            train_df["days_ago"] = (test_kickoff - train_df["kickoff"]).dt.days
            train_df = train_df.drop(columns=["kickoff"])
            try:
                model = DixonColesModel.fit(train_df, xi=xi, l2=l2)
            except Exception as exc:
                print(f"[{league_code}] fit failed at i={i}: {exc}")
                continue
            last_refit = i

        _process_prediction(
            test_match=matches[i],
            test_league=league_code,
            model=model,
            odds_index=odds_index,
            result=result,
            cal_state=cal_state,
            calibrate=calibrate,
            calib_min=calib_min,
            calib_refit_every=calib_refit_every,
            calib_window=calib_window,
            thresholds=thresholds,
            global_index=i,
        )

    return result


def backtest_pooled(
    db: Session,
    league_codes: list[str],
    min_train: int,
    refit_every: int,
    xi: float,
    l2: float,
    thresholds: list[float],
    odds_priority: list[str],
    calibrate: bool,
    calib_min: int = 100,
    calib_refit_every: int = 50,
    calib_window: int = 500,
) -> dict[str, LeagueResult]:
    """Walk forward over a single global timeline, fitting one pooled model.

    Each refit trains on every prior match across every requested league.
    Predictions are still made per match (using that match's league's gamma
    and delta), and bets are recorded into per-league `LeagueResult` buckets
    so the report can break ROI down per league.

    Calibration is also per-league: each league has its own rolling buffer
    and temperature, which is the right grain because mis-calibration
    intensities differed wildly across leagues in the per-league run
    (T_OU25 ranged 1.4 - 6.2).
    """
    # Load every league's matches and tag with the league code.
    all_matches: list[tuple[str, Match]] = []
    for code in league_codes:
        for m in load_league_matches(db, code):
            all_matches.append((code, m))
    all_matches.sort(key=lambda pair: pair[1].kickoff)

    if len(all_matches) < min_train + 10:
        print(f"[pooled] only {len(all_matches)} matches across {league_codes}; cannot run")
        return {code: LeagueResult(league=code) for code in league_codes}

    odds_index = load_odds_index(
        db, [m.id for _, m in all_matches], odds_priority
    )

    records = [
        {
            "kickoff": m.kickoff,
            "home_team": m.home_team.name,
            "away_team": m.away_team.name,
            "home_goals": m.ft_home,
            "away_goals": m.ft_away,
            "league": code,
        }
        for code, m in all_matches
    ]
    df_all = pd.DataFrame.from_records(records)

    results: dict[str, LeagueResult] = {code: LeagueResult(league=code) for code in league_codes}
    cal_states: dict[str, dict] = {code: _calibration_state() for code in league_codes}
    model: DixonColesModel | None = None
    last_refit: int = -1

    print(
        f"[pooled] {len(all_matches)} matches across {league_codes} — "
        f"walk-forward from index {min_train} (refit every {refit_every}; "
        f"l2={l2}; calibrate={calibrate})"
    )

    for i in range(min_train, len(all_matches)):
        if model is None or i - last_refit >= refit_every:
            train_df = df_all.iloc[:i].copy()
            test_kickoff = all_matches[i][1].kickoff
            train_df["days_ago"] = (test_kickoff - train_df["kickoff"]).dt.days
            train_df = train_df.drop(columns=["kickoff"])
            try:
                model = DixonColesModel.fit(train_df, xi=xi, l2=l2)
            except Exception as exc:
                print(f"[pooled] fit failed at i={i}: {exc}")
                continue
            last_refit = i
            if (i - min_train) % (refit_every * 5) == 0:
                # Light progress signal — pooled fits are slower than per-league.
                print(
                    f"  refit at i={i}/{len(all_matches)}  "
                    f"teams={len(model.teams)}  rho={model.rho:+.3f}"
                )

        code, test = all_matches[i]
        _process_prediction(
            test_match=test,
            test_league=code,
            model=model,
            odds_index=odds_index,
            result=results[code],
            cal_state=cal_states[code],
            calibrate=calibrate,
            calib_min=calib_min,
            calib_refit_every=calib_refit_every,
            calib_window=calib_window,
            thresholds=thresholds,
            global_index=i,
        )

    return results


# ----------------------- reporting -----------------------


def calibration_table(points: list[tuple[float, int]], n_bins: int = 10) -> list[dict]:
    if not points:
        return []
    df = pd.DataFrame(points, columns=["prob", "hit"])
    df["bin"] = pd.cut(df["prob"], bins=np.linspace(0, 1, n_bins + 1), include_lowest=True)
    grouped = df.groupby("bin", observed=True).agg(
        n=("hit", "size"),
        predicted=("prob", "mean"),
        actual=("hit", "mean"),
    )
    return [
        {
            "bin": str(idx),
            "n": int(row["n"]),
            "predicted": float(row["predicted"]),
            "actual": float(row["actual"]),
            "gap": float(row["actual"] - row["predicted"]),
        }
        for idx, row in grouped.iterrows()
    ]


def report_bets(bets: list[Bet], thresholds: list[float]) -> list[dict]:
    rows: list[dict] = []
    for market in sorted({b.market for b in bets}):
        market_bets = [b for b in bets if b.market == market]
        for t in thresholds:
            filtered = [b for b in market_bets if b.edge > t]
            n = len(filtered)
            if n == 0:
                rows.append(
                    {"market": market, "threshold": t, "n": 0, "win_rate": None, "roi": None}
                )
                continue
            wins = sum(1 for b in filtered if b.won)
            pnl = sum(b.pnl for b in filtered)
            rows.append(
                {
                    "market": market,
                    "threshold": t,
                    "n": n,
                    "win_rate": wins / n,
                    "roi": pnl / n,
                }
            )
    return rows


def print_report(results: list[LeagueResult], thresholds: list[float], odds_label: str) -> None:
    print("\n" + "=" * 70)
    print("PER-LEAGUE SUMMARY")
    print("=" * 70)
    print(f"{'League':10s} {'N':>6s} {'Brier':>8s} {'LogLoss':>9s} {'Bets':>6s} {'ROI@best':>10s}")
    for r in results:
        if r.n_predictions == 0:
            continue
        rows = report_bets(r.bets, thresholds)
        best_roi = None
        for row in rows:
            if row["roi"] is not None and row["n"] >= 30:
                if best_roi is None or row["roi"] > best_roi:
                    best_roi = row["roi"]
        roi_str = f"{best_roi*100:+.1f}%" if best_roi is not None else "  —  "
        print(
            f"{r.league:10s} {r.n_predictions:6d} {r.brier:8.4f} {r.logloss:9.4f} "
            f"{len(r.bets):6d} {roi_str:>10s}"
        )

    # Aggregate
    all_points: list[tuple[float, int]] = []
    all_bets: list[Bet] = []
    total_n = 0
    total_brier = 0.0
    total_logloss = 0.0
    for r in results:
        all_points.extend(r.calibration_points)
        all_bets.extend(r.bets)
        total_n += r.n_predictions
        total_brier += r.brier_sum
        total_logloss += r.logloss_sum

    if total_n == 0:
        print("\nNo predictions were made — did you ingest any matches first?")
        return

    print("\n" + "=" * 70)
    print("OVERALL")
    print("=" * 70)
    print(f"  Predictions:  {total_n}")
    print(f"  Brier score:  {total_brier/total_n:.4f}   (lower is better; < 0.19 is strong)")
    print(f"  Log loss:     {total_logloss/total_n:.4f}")

    print("\nCALIBRATION (predicted vs actual win rate, per 1X2 probability bucket)")
    print(f"  {'bucket':25s} {'n':>6s} {'predicted':>10s} {'actual':>10s} {'gap':>8s}")
    for row in calibration_table(all_points):
        print(
            f"  {row['bin']:25s} {row['n']:6d} {row['predicted']:10.3f} "
            f"{row['actual']:10.3f} {row['gap']:+8.3f}"
        )

    # Correct-score quality: are the top-K predictions calibrated?
    total_t1_p = sum(r.cs_top1_prob_sum for r in results)
    total_t1_h = sum(r.cs_top1_hits for r in results)
    total_t3_p = sum(r.cs_top3_prob_sum for r in results)
    total_t3_h = sum(r.cs_top3_hits for r in results)
    total_t5_p = sum(r.cs_top5_prob_sum for r in results)
    total_t5_h = sum(r.cs_top5_hits for r in results)
    if total_n > 0:
        print("\nCORRECT-SCORE QUALITY (top-K cumulative probability vs actual hit rate)")
        print(f"  {'K':>3s} {'predicted':>10s} {'actual':>10s} {'gap':>8s} {'edge@fair':>11s}")
        for k, p_sum, hits in (
            (1, total_t1_p, total_t1_h),
            (3, total_t3_p, total_t3_h),
            (5, total_t5_p, total_t5_h),
        ):
            pred = p_sum / total_n
            act = hits / total_n
            gap = act - pred
            # If you bet each top-K cell at "fair" odds (1/prob) and win when actual
            # hits, expected ROI per unit stake = (hits / sum_of_probs) - 1. This is
            # the ROI you'd get if bookmakers priced correct-score with zero vig. Any
            # real book prices above fair, so crossing this line is necessary but not
            # sufficient for a real edge; still, it's a pure model-quality signal.
            edge_at_fair = (hits / p_sum) - 1.0 if p_sum > 0 else float("nan")
            print(
                f"  {k:3d} {pred:10.3f} {act:10.3f} {gap:+8.3f} {edge_at_fair*100:+10.1f}%"
            )

    print(f"\nBET SIMULATION (1-unit stake per bet, at {odds_label} odds)")
    print(
        f"  {'market':10s} {'edge>':>7s} {'n':>6s} {'win%':>8s} {'ROI':>9s}"
    )
    for row in report_bets(all_bets, thresholds):
        n = row["n"]
        if n == 0:
            print(
                f"  {row['market']:10s} {row['threshold']:7.2f} {n:6d} {'—':>8s} {'—':>9s}"
            )
            continue
        print(
            f"  {row['market']:10s} {row['threshold']:7.2f} {n:6d} "
            f"{row['win_rate']*100:7.1f}% {row['roi']*100:+8.1f}%"
        )


# ----------------------- entry point -----------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Walk-forward backtest of Dixon-Coles.")
    parser.add_argument(
        "--leagues",
        default=None,
        help="Comma-separated league codes. Default: every ingested league.",
    )
    parser.add_argument("--min-train", type=int, default=200)
    parser.add_argument("--refit-every", type=int, default=20)
    parser.add_argument("--xi", type=float, default=0.0018, help="Time-decay parameter.")
    parser.add_argument(
        "--thresholds",
        default="0,0.03,0.05,0.10",
        help="Comma-separated edge thresholds to report ROI at.",
    )
    parser.add_argument(
        "--odds",
        choices=("closing", "opening"),
        default="closing",
        help=(
            "Which odds to bet against. 'closing' is the efficient-market reference; "
            "'opening' is realistic for a bettor placing days before kickoff."
        ),
    )
    parser.add_argument(
        "--calibrate",
        action="store_true",
        help="Apply rolling temperature scaling to model outputs (post-hoc calibration).",
    )
    parser.add_argument(
        "--pool",
        action="store_true",
        help=(
            "Fit a single Dixon-Coles model jointly across all requested leagues "
            "(per-league gamma + delta, shared rho, per-team alpha/beta). "
            "Walk-forward runs over a single global timeline."
        ),
    )
    parser.add_argument(
        "--l2",
        type=float,
        default=0.0,
        help=(
            "Ridge penalty on alpha/beta. Shrinks team strengths toward zero — "
            "use this to combat tail overconfidence. Try 1, 5, 20."
        ),
    )
    args = parser.parse_args()

    thresholds = sorted(float(t) for t in args.thresholds.split(","))
    odds_priority = OPENING_PRIORITY if args.odds == "opening" else CLOSING_PRIORITY

    with SessionLocal() as db:
        if args.leagues:
            codes = args.leagues.split(",")
        else:
            # Only leagues that actually have ingested data
            codes = [
                c
                for c in (spec.code for spec in MAIN_LEAGUES + NEW_LEAGUES)
                if db.scalar(select(Match.id).where(Match.league == c).limit(1)) is not None
            ]
        if not codes:
            raise SystemExit("No ingested leagues found. Run scripts/ingest_all.py first.")

        print(
            f"Backtesting leagues: {codes}  "
            f"(odds: {args.odds}, calibrate: {args.calibrate}, "
            f"pool: {args.pool}, l2: {args.l2})"
        )
        if args.pool:
            pooled = backtest_pooled(
                db,
                codes,
                min_train=args.min_train,
                refit_every=args.refit_every,
                xi=args.xi,
                l2=args.l2,
                thresholds=thresholds,
                odds_priority=odds_priority,
                calibrate=args.calibrate,
            )
            results = [pooled[code] for code in codes]
        else:
            results = [
                backtest_league(
                    db,
                    code,
                    min_train=args.min_train,
                    refit_every=args.refit_every,
                    xi=args.xi,
                    l2=args.l2,
                    thresholds=thresholds,
                    odds_priority=odds_priority,
                    calibrate=args.calibrate,
                )
                for code in codes
            ]

    print_report(results, thresholds, odds_label=args.odds)
    if args.calibrate:
        print("\nCALIBRATION TEMPERATURES (final fit per league)")
        print(f"  {'League':10s} {'fits':>6s} {'T_1X2':>8s} {'T_OU25':>8s}")
        for r in results:
            if r.n_predictions == 0:
                continue
            print(
                f"  {r.league:10s} {r.n_calibrator_fits:6d} "
                f"{r.final_T_1x2:8.3f} {r.final_T_ou25:8.3f}"
            )
        print(
            "  T > 1: model was overconfident and got softened. "
            "T < 1: model was under-confident and got sharpened."
        )


if __name__ == "__main__":
    main()
