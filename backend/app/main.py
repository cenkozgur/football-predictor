from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import auth, coupon_history, coupons, matches, predictions, sport_events, stats
from app.db import Base, engine
from app import models  # noqa: F401 — registers tables on Base.metadata

# No Alembic in this project; create_all is idempotent and safe to call on
# every boot (it no-ops for tables that already exist). Lets us ship new
# tables — like coupons / coupon_legs — without a manual migration step.
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="Football Predictor API",
    description="Multi-user football match predictor covering top European leagues, UEFA, and World Cup.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten before production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(matches.router, prefix="/matches", tags=["matches"])
app.include_router(predictions.router, prefix="/predictions", tags=["predictions"])
app.include_router(coupons.router, prefix="/coupons", tags=["coupons"])
app.include_router(coupon_history.router, prefix="/coupons", tags=["coupons"])
app.include_router(stats.router, prefix="/stats", tags=["stats"])
app.include_router(sport_events.router, prefix="/sport-events", tags=["sport-events"])


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
