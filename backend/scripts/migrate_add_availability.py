"""One-shot migration: ensure team_availability table exists.

Idempotent. Project uses `Base.metadata.create_all(bind=engine)` at boot to
auto-create missing tables, but workflow scripts (like fetch_availability)
may run before a FastAPI boot on a fresh DB — this guarantees the table is
present regardless of call order.
"""

from __future__ import annotations

from app.db import Base, engine
from app.models.team_availability import TeamAvailability  # noqa: F401  (register table)


def main() -> None:
    Base.metadata.create_all(bind=engine, tables=[TeamAvailability.__table__])
    print("team_availability table ensured.")


if __name__ == "__main__":
    main()
