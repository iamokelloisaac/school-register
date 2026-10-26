"""
scripts/seed.py
===============

Bootstrap script: create the initial admin and a few sample programs.

Run this once, after migrations, on a fresh database. Re-running is safe —
every insert is idempotent (checks existence first, or uses a UNIQUE
constraint to no-op).

## Why a seed script exists at all

Two reasons, and both are worth understanding:

  1. **The first admin has to come from somewhere.** The registration
     endpoint always creates `role='student'` — deliberately, so a client
     can't self-promote. That means admin accounts can't be created
     through the API at all. They need to be inserted directly, and a
     script is the honest way to do it.

  2. **Sample data makes the app testable by hand.** Open `/docs`, hit
     `GET /programs`, and see something — instead of an empty list and
     wondering whether the endpoint is broken or the table is just empty.

## What this script does NOT do

  - It doesn't run migrations. Those are `.sql` files applied with
    `psql`, one level down. The seed assumes the schema exists and
    fails loudly if it doesn't.

  - It doesn't reset anything. Running it on a populated database is a
    no-op (or a targeted add), never a wipe. If you want a clean slate,
    drop the DB and re-run migrations, then run this.

## How to run it

From the project root, with the venv active:

    python -m scripts.seed

Note the `-m` flag — the script is a *module*, and running it as a
module means relative imports work and `sys.path` includes the project
root. Running `python scripts/seed.py` directly would fail on the
`from app...` imports, because `sys.path[0]` would be `scripts/` instead
of the project root.

## The admin's password

For a learning project, we hardcode a default password. **This is not a
production pattern.** In a real deployment, you'd either:

  - prompt for the password interactively (using `getpass`), or
  - generate a random one and print it once, or
  - create the admin with no password and force a reset flow on first
    login.

We do the simple thing and the docstring says why. The password is
`changeme123` — obviously bad, on purpose, so nobody accidentally ships
it.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from app.core import security
from app.core.config import settings
from app.core.logging import configure_logging
from app.db.pool import close_pool, open_pool, get_pool
from app.db.queries import programs as programs_q
from app.db.queries import users as users_q

logger = logging.getLogger("scripts.seed")


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------
#
# The admin's credentials. Change these if you want; the script checks
# by email, so re-running with different values creates a *second* admin
# rather than modifying the first. That's intentional — an admin you
# created with the wrong email is a mistake to fix deliberately, not
# something to overwrite silently.

ADMIN_EMAIL = "admin@school.local"
ADMIN_PASSWORD = "changeme123"
ADMIN_FULL_NAME = "System Administrator"
ADMIN_AGE = 30


# Sample programs. Each is (name, code, description). The `code` values
# are uppercased here and validated by `ProgramCreate` when going through
# the API — but we're bypassing the schema and inserting via the query
# layer, so this script is responsible for the same normalization the
# schema would do. Keeping codes uppercase matches what a real admin
# would type.

SAMPLE_PROGRAMS: list[tuple[str, str, str | None]] = [
    ("Computer Science", "CS", "Algorithms, systems, and software engineering."),
    ("Mathematics", "MATH", "Pure and applied mathematics."),
    ("Physics", "PHYS", "Classical and modern physics."),
]


# ---------------------------------------------------------------------------
# The script
# ---------------------------------------------------------------------------


async def seed_admin() -> None:
    """
    Create the admin account if it doesn't already exist.

    Idempotent: if a user with `ADMIN_EMAIL` is already present, we log
    and return. The check is a `SELECT` followed by an `INSERT`, which
    is *not* atomic — but since this is a bootstrap script, not a
    concurrent API endpoint, the race doesn't matter. If we ever run the
    seed in parallel (we won't), the UNIQUE constraint on email would
    still catch the collision, and the script would fail loudly with a
    `UniqueViolation`. That's the correct failure mode.
    """
    async with get_pool().connection() as conn:
        existing = await users_q.get_user_by_email(conn, ADMIN_EMAIL)

        if existing is not None:
            logger.info("admin already exists: %s (id=%s)", ADMIN_EMAIL, existing["id"])
            return

        hashed = security.hash_password(ADMIN_PASSWORD)

        admin = await users_q.create_user(
            conn,
            email=ADMIN_EMAIL,
            hashed_password=hashed,
            full_name=ADMIN_FULL_NAME,
            age=ADMIN_AGE,
            role="admin",  # the one field that makes this a bootstrap
        )

        logger.info("created admin: %s (id=%s)", ADMIN_EMAIL, admin["id"])


async def seed_programs() -> None:
    """
    Create the sample programs if they don't already exist.

    Idempotent by checking each program's code first. Same non-atomic
    caveat as `seed_admin` — doesn't matter for a bootstrap script.

    Each program is created by the admin, so `created_by` is the admin's
    id. If the admin wasn't seeded (because the admin seeding failed),
    this function will fail on the foreign key constraint. That's the
    correct order-dependency: we call `seed_admin` first.
    """
    async with get_pool().connection() as conn:
        # Look up the admin so we can attribute the programs to them.
        # If this fails, the admin wasn't created — the script's top-
        # level error handling will report that.
        admin = await users_q.get_user_by_email(conn, ADMIN_EMAIL)

        if admin is None:
            # Should be impossible: we call seed_admin first, and it
            # raises on failure. Defensive check for clarity.
            logger.error("cannot seed programs: admin %s not found", ADMIN_EMAIL)
            raise RuntimeError("admin not found after seed step")

        for name, code, description in SAMPLE_PROGRAMS:
            existing = await programs_q.get_program_by_code(conn, code)

            if existing is not None:
                logger.info("program already exists: %s (%s)", code, name)
                continue

            program = await programs_q.create_program(
                conn,
                name=name,
                code=code,
                description=description,
                created_by=admin["id"],
            )

            logger.info("created program: %s (id=%s)", code, program["id"])


async def main() -> None:
    """
    Orchestrate: configure logging, open the pool, seed admin, seed
    programs, close the pool.

    The structure mirrors `main.py`'s lifespan — same open/close
    discipline — because the pool is a shared resource and cleaning up
    after ourselves is what makes the script safe to run repeatedly
    against the same process (and to Ctrl+C without leaking
    connections).

    We use `sys.exit(1)` on failure so the script's exit code reflects
    the outcome. A script that fails but exits 0 is a small crime
    against automation — it lies about success. Even for a manual
    script, this habit is worth forming.

    No `try/except` around the whole thing — if a step raises, we want
    the traceback visible. The `finally` around pool closing is what
    matters for cleanup.
    """
    configure_logging()

    logger.info("seeding database: %s", settings.DATABASE_URL.split("@")[-1])
    # (We log only the host/db portion, not the full URL with credentials.
    # Never log connection strings that include passwords.)

    await open_pool()
    try:
        await seed_admin()
        await seed_programs()
        logger.info("seed complete")
    finally:
        await close_pool()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Ctrl+C during a seed. Not an error; exit cleanly.
        sys.exit(130)
    except Exception as exc:
        # Log the failure with a traceback, then exit non-zero so any
        # caller (a CI job, a Makefile) sees the failure.
        logging.getLogger("scripts.seed").exception("seed failed: %s", exc)
        sys.exit(1)