# Roadmap

## Phase 1 — Data foundation + baseline model (this scaffold)

- [x] PostgreSQL schema (users, teams, matches, odds, predictions)
- [x] FastAPI skeleton with JWT auth
- [x] Dixon-Coles bivariate Poisson implementation
- [x] Markets derivation module (1X2, O/U, BTTS, correct score, AH, odd/even, double chance, goal range)
- [x] Value engine (edge + fractional Kelly + tagging)
- [x] Tests for markets derivation
- [x] Training script (`scripts/train_baseline.py`)
- [x] Leagues catalog covering all European top flights (11 main + 10 new leagues)
- [x] football-data.co.uk main-format ingester (results + closing odds from B365C / PSC)
- [x] football-data.co.uk new-leagues-format ingester (aggregated per-country)
- [x] Bulk ingest CLI (`scripts/ingest_all.py`)
- [x] Walk-forward backtester with Brier, log loss, calibration, and ROI at multiple thresholds (`scripts/backtest.py`)
- [ ] Run the backtest across 5 seasons × all European top flights and record baseline numbers here
- [ ] If baseline ROI < 3%, move to Phase 4 (ensemble + calibration) before building the frontend

## Phase 2 — Multi-market + half-split

- [ ] Half-split Poisson model for HT, HT/FT, 2H markets
- [ ] First-goal time-to-event model
- [ ] Add markets to `markets.py`: HT 1X2, HT O/U, HT/FT combos
- [ ] Prediction endpoint returns the full multi-market blob

## Phase 3 — Bilyoner integration + value UX

- [ ] Polite bilyoner odds scraper (respect robots, cache aggressively, user-agent)
- [ ] Odds ingestion loop (every N minutes for today's fixtures)
- [ ] Value engine endpoint: `GET /predictions/value?mode=banko|kombine|all`
- [ ] Web frontend (Next.js + Tailwind) with two tabs
- [ ] Mobile frontend (Expo) with push notifications on new value

## Phase 4 — Ensemble + calibration

- [ ] Engineer features: rolling xG, ClubElo, rest days, home/away splits, travel
- [ ] LightGBM 1X2 model on engineered features
- [ ] Bayesian hierarchical model (PyMC) for small-sample competitions
- [ ] Stacking meta-learner with isotonic calibration
- [ ] MLflow experiment tracking

## Phase 5 — UEFA + World Cup

- [ ] Cross-league strength normalization (teams with few matches vs each other)
- [ ] UCL / UEL / UECL ingestion + feature engineering
- [ ] World Cup module (activates around qualifying)

## Phase 6 — Extra markets

- [ ] Corners model (separate Poisson, different feature set — possession, block depth)
- [ ] Cards model (referee-conditioned)
- [ ] Player props (goalscorer, assists — needs lineup confirmations 1h pre-match)

## Phase 7 — Polish

- [ ] Per-user bankroll tracking + ROI dashboard
- [ ] Closing line value (CLV) monitoring
- [ ] Responsible-gambling features: loss caps, cool-off mode
- [ ] Backtesting UI
