"""One-shot migration: add xg_home / xg_away columns to matches.

Idempotent — safe to run multiple times. Needed because this project uses
`Base.metadata.create_all` (no Alembic), which doesn't alter existing tables.
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
        if "xg_home" not in existing:
            conn.execute(text("ALTER TABLE matches ADD COLUMN xg_home FLOAT"))
            added.append("xg_home")
        if "xg_away" not in existing:
            conn.execute(text("ALTER TABLE matches ADD COLUMN xg_away FLOAT"))
            added.append("xg_away")
        if added:
            print(f"Added columns: {', '.join(added)}")
        else:
            print("Columns already present — no-op.")


if __name__ == "__main__":
    main()
