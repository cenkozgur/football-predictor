# Architecture

## Design principles

1. **One score matrix unlocks many markets.** A single calibrated bivariate Poisson model gives you the joint distribution P(home_goals = i, away_goals = j). From that matrix you can derive 1X2, double chance, over/under (any line), BTTS, correct score, Asian handicap, odd/even, and goal range *analytically* — no separate model needed per market. This is the architectural keystone.
2. **Value, not accuracy.** The goal is to find selections where `model_prob × bilyoner_odds − 1 > threshold`. Calibration matters more than raw accuracy.
3. **Two modes, one pipeline.** Both the "predict every game" view and the "value bets only" view come from the same predictions — they are just different filters.
4. **Ensemble over single model.** Dixon-Coles is the baseline. LightGBM (on engineered features) and a Bayesian hierarchical model for small-sample competitions (UECL, World Cup) are added on top, stacked with isotonic calibration.
5. **DIY data first.** Free, public sources are enough to get started: football-data.co.uk for historical results + closing odds, Understat for xG, ClubElo for daily ratings, and scraped bilyoner odds for value calculation.

## Layers

### 1. Ingestion (`app/ingestion/`)

- `base.py` — shared HTTP client with retries, rate limiting, and polite user-agent.
- `football_data.py` — downloads historical match-level CSVs from football-data.co.uk (results, FT/HT scores, and closing odds from multiple books).
- `understat.py` — scrapes match-level xG/xGA (planned).
- `clubelo.py` — daily ClubElo ratings (planned).
- `bilyoner.py` — live odds scrape for the value engine (planned, polite scraping only).

### 2. Feature store (`app/models/`)

PostgreSQL tables:

- `users` — auth (email + hashed password)
- `teams` — id, name, league, country, current Elo
- `matches` — fixtures with FT and HT scores once played
- `odds` — time-series of odds per market, per source
- `predictions` — model outputs keyed by match + model version

### 3. Model layer (`app/ml/`)

- `dixon_coles.py` — hand-rolled Dixon-Coles bivariate Poisson with low-score correction. Fit via scipy maximum likelihood.
- `markets.py` — derives every market analytically from the score matrix. The single most important file in the project.
- `value.py` — edge calculation, fractional Kelly sizing, banko / kombine tagging.

Later phases add:
- `half_split.py` — separate first-half Poisson for HT, HT/FT, 2H markets
- `lightgbm_1x2.py` — gradient boosting 1X2 refinement with engineered features
- `bayesian_hierarchical.py` — PyMC model for UECL / World Cup small samples
- `ensemble.py` — stacks all models with isotonic calibration

### 4. Value engine (`app/ml/value.py`)

For every (match, market, selection) triple:

```
edge = model_prob × bilyoner_odds − 1
kelly_fraction = ((model_prob × odds) − 1) / (odds − 1)   # full Kelly
stake = bankroll × 0.25 × max(0, kelly_fraction)          # ¼ Kelly
```

Classification rules:

| Tag | Criteria |
|---|---|
| `banko_value` | `model_prob ≥ 0.70` AND `edge ≥ 0.03` |
| `kombine_value` | `0.35 ≤ model_prob < 0.70` AND `edge ≥ 0.05` |
| `no_value` | otherwise |

### 5. API (`app/api/`)

- `auth.py` — register, login, JWT tokens
- `matches.py` — list upcoming fixtures, get match detail
- `predictions.py` — full predictions for a match, value-filtered predictions with mode query param (`mode=all|banko|kombine`)

### 6. Frontend (separate phase)

- `web/` — Next.js + Tailwind, two tabs: **Tüm Tahminler** / **Değer Bahisler**
- `mobile/` — React Native (Expo), same two tabs + push notifications when a new value bet appears

## Why Dixon-Coles

The vanilla independent-Poisson model over-predicts 1-1, 2-2, and under-predicts 0-0, 1-0, 0-1 — exactly the low-score cells where most football matches live. Dixon-Coles adds a four-cell correction parameter `ρ` that fits football's empirical score distribution well without adding meaningful complexity.

The `τ` correction:

```
τ(0,0) = 1 − λμρ
τ(0,1) = 1 + λρ
τ(1,0) = 1 + μρ
τ(1,1) = 1 − ρ
τ(i,j) = 1   otherwise
```

Fit by maximum likelihood with an identifiability constraint (`mean(attack) = 0`) and an exponential time-decay weight `exp(−ξ · days_ago)` so recent matches count more.

## Why multi-user

Even if you only use this personally at first, building multi-user from day one costs almost nothing and unlocks:
- Per-user bankroll tracking (stake sizing is personal)
- Per-user pick history and ROI
- Per-user notification preferences
- Eventually: inviting friends, or public performance tracking

## What's not in Phase 1

- Corners, cards, player props (need data sources we don't have yet)
- HT/FT markets (need the half-split model)
- LightGBM ensemble (baseline first)
- Frontend (placeholders only — we want the backend rock-solid first)
- Bilyoner scraper (polite scrape planned for Phase 3 after the model is calibrated)
