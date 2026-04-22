"""One-shot migration: add live_minute / live_home / live_away to matches.

Idempotent. Needed because this project uses `Base.metadata.create_all`
(no Alembic), which doesn't alter existing tables — a fresh ORM query
against these columns would fail on prod until they physically exist.
"""

from __future__ import annotations

from sqlalchemy import text

from app.db import engine


def main() -> None:
    with engine.begin() as conn:
        existing = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(matches)")).all()
        }
        added = []
        for col in ("live_minute", "live_home", "live_away"):
            if col not in existing:
                conn.execute(text(f"ALTER TABLE matches ADD COLUMN {col} INTEGER"))
                added.append(col)
        if added:
            print(f"Added columns: {', '.join(added)}")
        else:
            print("Columns already present — no-op.")


if __name__ == "__main__":
    main()
