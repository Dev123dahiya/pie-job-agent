"""Populate the configured database with mock data: `python -m backend.scripts.seed_mock`.

Refuses to run unless DEVELOPMENT_MODE=true, and refuses to seed a non-empty database.
"""

import sys

from backend.core.config import get_settings
from backend.services.mock_data import seed_mock_data
from database.repositories import JobRepository
from database.session import get_session_factory


def main() -> int:
    if not get_settings().development_mode:
        print("Refusing to seed mock data: DEVELOPMENT_MODE is not enabled.", file=sys.stderr)
        return 1
    with get_session_factory()() as db:
        if JobRepository(db).count():
            print("Database already contains jobs; not seeding.", file=sys.stderr)
            return 1
        seed_mock_data(db)
    print("Mock data seeded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
