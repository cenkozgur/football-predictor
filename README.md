# Football Predictor

A multi-user football match prediction platform covering the top European leagues, UEFA Champions League, UEFA Europa League, UEFA Conference League, and the FIFA World Cup.

Built for the Turkish market (bilyoner.com pricing reference) with predictions across every major betting market — 1X2, double chance, over/under, BTTS (KG Var/Yok), correct score, HT/FT, Asian handicap, corners, cards, and more — all derived from a single calibrated probabilistic model per match.

## Two output modes

1. **Tahmin (Prediction view)** — every market, every match, always shown. Useful for exploration and learning.
2. **Değer (Value view)** — only selections where the model's probability × bilyoner odds > 1 (positive expected value), split into:
   - **Banko değer** — high probability (≥ 0.70) + positive edge → singles
   - **Kombine değer** — medium probability, higher odds → coupon builder

## Architecture at a glance

```
Ingestion ─▶ Feature store ─▶ Model layer ─▶ Value engine ─▶ API ─▶ Web + Mobile
```

- **Ingestion** — DIY scrapers for football-data.co.uk (historical results + odds), Understat (xG), ClubElo, bilyoner (live odds). No paid APIs.
- **Feature store** — PostgreSQL with match-level and team-level rolling features.
- **Model layer** — Dixon-Coles bivariate Poisson as the baseline (derives ~15 markets from one score matrix), ensembled later with LightGBM and Bayesian hierarchical models.
- **Value engine** — edge calculation vs bilyoner odds, fractional Kelly sizing, banko / kombine classification.
- **API** — FastAPI, JWT auth, multi-user.
- **Frontend** — Next.js web + React Native (Expo) mobile (scaffolded in Phase 3).

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full design and [docs/ROADMAP.md](docs/ROADMAP.md) for the phased plan.

## Leagues covered (top flight)

**Main format** (per-season CSVs, rich schema with HT scores and many bookmakers):

| Code | Country | League |
|------|---------|--------|
| E0   | England | Premier League |
| D1   | Germany | Bundesliga |
| I1   | Italy | Serie A |
| SP1  | Spain | La Liga |
| F1   | France | Ligue 1 |
| N1   | Netherlands | Eredivisie |
| B1   | Belgium | Jupiler Pro League |
| P1   | Portugal | Primeira Liga |
| T1   | Turkey | Süper Lig |
| G1   | Greece | Super League |
| SC0  | Scotland | Premiership |

**New-leagues format** (aggregated per-country CSVs, Pinnacle + Avg/Max odds):

| Code | Country | League |
|------|---------|--------|
| AUT  | Austria | Bundesliga |
| DNK  | Denmark | Superliga |
| FIN  | Finland | Veikkausliiga |
| IRL  | Ireland | Premier Division |
| NOR  | Norway | Eliteserien |
| POL  | Poland | Ekstraklasa |
| ROU  | Romania | Liga I |
| RUS  | Russia | Premier League |
| SWE  | Sweden | Allsvenskan |
| SWZ  | Switzerland | Super League |

UCL / UEL / UECL and the World Cup need different data sources and are planned for Phase 5.

## Quick start

```bash
# 1. Start infra
docker compose up -d

# 2. Install backend deps (requires Python 3.12+)
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 3. Initialize the database
cp .env.example .env
python -m app.db init

# 4. Run the markets tests — proves the math is internally consistent.
pytest

# 5. Bulk-ingest every European top flight for the last 5 seasons
python scripts/ingest_all.py
# Or, for a quick start:
python scripts/ingest_all.py --seasons 2 --only E0,D1,I1,SP1,F1

# 6. Walk-forward backtest (the critical moment of truth)
python scripts/backtest.py
# Scope to one league for a ~1-minute run:
python scripts/backtest.py --leagues E0 --min-train 200

# 7. Train a baseline on the latest data and see a sample multi-market prediction
python scripts/train_baseline.py --league E0 --home "Man City" --away "Arsenal"

# 8. Run the API
uvicorn app.main:app --reload
```

Then open http://localhost:8000/docs for the interactive Swagger UI.

## Reading the backtest report

The backtest report is the only thing that tells you whether the model has an edge. Look for:

- **Brier score** — < 0.19 is strong, 0.19–0.21 is acceptable, > 0.21 means the model is poorly calibrated.
- **Calibration table** — the "gap" column should be near zero in every bucket. Positive gaps mean the model under-predicts; negative gaps mean it over-predicts. Large gaps justify adding isotonic calibration (Phase 4).
- **ROI per edge threshold** — at threshold 0 you'll often see negative ROI (bookmaker margin). As you raise the threshold you're only betting when the model disagrees with the market *a lot*. If ROI stays negative even at 0.05+, the model's edge is noise. Look for **ROI ≥ 3% at threshold 0.05 with n ≥ 300 bets** — that's the threshold for "the model is worth deploying."

## Responsible use

This project is for personal, educational, and research use. Betting involves real financial risk — no model guarantees profit. Track ROI and closing line value, not win rate. Use fractional Kelly (¼ or ⅛), set loss limits, and stop when the model's calibration degrades.
