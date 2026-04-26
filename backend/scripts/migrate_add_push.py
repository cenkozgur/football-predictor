"""Idempotent: ensure push_subscriptions table exists.

Same pattern as scripts/migrate_add_availability.py. main.py's
create_all() runs on boot anyway, but workflow scripts (e.g.
resolve_coupons hooked to push) may execute before any FastAPI boot
on a fresh DB snapshot, so we make the table eagerly here.
"""

from __future__ import annotations

from app.db import Base, engine
from app.models.push import PushSubscription  # noqa: F401  (register table)


def main() -> None:
    Base.metadata.create_all(bind=engine, tables=[PushSubscription.__table__])
    print("push_subscriptions table ensured.")


if __name__ == "__main__":
    main()
