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


@app.get("/_debug/db")
def _debug_db() -> dict:
    import os
    from sqlalchemy import text
    from app.config import get_settings
    from app.db import engine

    s = get_settings()
    info: dict = {"database_url": s.database_url}
    for p in ["/app/football.db", "./football.db", "football.db"]:
        try:
            info[p] = {"exists": os.path.exists(p), "size": os.path.getsize(p) if os.path.exists(p) else 0}
        except Exception as e:
            info[p] = {"error": str(e)}
    try:
        with engine.connect() as conn:
            info["matches_count"] = conn.execute(text("SELECT COUNT(*) FROM matches")).scalar()
            info["scheduled_future"] = conn.execute(
                text("SELECT COUNT(*) FROM matches WHERE status='scheduled' AND kickoff > datetime('now')")
            ).scalar()
    except Exception as e:
        info["query_error"] = str(e)
    info["cwd"] = os.getcwd()
    return info
