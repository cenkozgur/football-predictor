from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import auth, coupons, matches, predictions, stats

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
app.include_router(stats.router, prefix="/stats", tags=["stats"])


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
