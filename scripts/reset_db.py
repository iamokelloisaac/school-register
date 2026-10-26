"""
scripts/reset_db.py
===================

Dev-only script: drop the schema and re-run all migrations.

## What "reset" means here

Drop every table we own (and the enum type, and the CITEXT extension),
then re-apply the four migration files in order. The result is a
schema identical to what a fresh `psql` run of the migrations would
produce — no partial state, no leftover data.

## Why this exists

During development you'll often want a clean slate: testing seed
changes, verifying migrations from scratch, or clearing out data that
piled up from debugging. Doing it by hand is a sequence of `DROP TABLE`
statements plus a loop over `migrations/*.sql`. This script is that
sequence, in the right order, with a safety check.

## Safety

Two guardrails:

  1. **It refuses to run when `ENV=prod`.** The check is on the `ENV`
     setting, not on the database name. A production deployment with
     `ENV=prod` in `.env` will be rejected even if someone points it at
     the right database — belt and suspenders.

  2. **It requires an explicit confirmation flag.** `--yes` on the
     command line. Default behavior is to print what it would do and
     exit. This is the "you typed the wrong thing" protection.

## What it does NOT do

  - It does not run `DROP DATABASE` or `CREATE DATABASE`. Dropping the
    database itself requires disconnecting all clients and is
    overkill — dropping the schema's objects is enough. The database
    stays, its contents don't.

  - It does not touch the test database. That has its own bootstrap
    script (`scripts/init_test_db.sh`). This script is for the dev
    database only.

  - It does not seed. Run `scripts/seed.py` after if you want sample
    data.

## How to run it

    python -m scripts.reset_db --yes

Without `--yes`, it prints what would happen and exits with code 0.
This is deliberate: dry-run as the default is the safer choice, and
typing the flag is a small ceremony that prevents muscle-memory
mistakes.

The `-m` flag is required (as with the seed script) so that `from
app...` imports resolve. See `scripts/seed.py` for the fuller
explanation of why.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from app.core.config import settings
from app.core.logging import configure_logging
from app.db.pool import close_pool, get_pool, open_pool

logger = logging.getLogger("scripts.reset_db")


# ---------------------------------------------------------------------------
# What gets dropped
# ---------------------------------------------------------------------------
#
# Order matters: `DROP TABLE` with foreign keys needs to go leaf-first,
# or use `CASCADE` to handle dependencies. We use `CASCADE` so the order
# in this list doesn't matter, but we list them in dependency order
# (leaf first, root last) as a readability convention — it makes the
# relationship between tables obvious.

TABLES_IN_DROP_ORDER = [
    "enrollments",   # leaf — FKs to users, programs
    "sessions",      # leaf — FK to users
    "programs",      # refers to users
    "users",         # root
]

# The enum type is dropped after the tables that use it. Dropping a
# type in use would fail; since all tables referencing `user_role` are
# gone by this point, the drop is safe.

TYPES_TO_DROP = ["user_role"]

# Postgres extensions have to be dropped explicitly if we want the
# reset to remove every trace. CITEXT is the only one we install. In
# practice, we could leave it — `CREATE EXTENSION IF NOT EXISTS` is a
# no-op if it's already present. But dropping it makes the reset truly
# complete, which is what "reset" should mean.

EXTENSIONS_TO_DROP = ["citext"]


# ---------------------------------------------------------------------------
# Migration files
# ---------------------------------------------------------------------------


def find_migrations() -> list[Path]:
    """
    Return the migration files in sorted order.

    The migrations live in `migrations/` at the project root. We sort
    by filename, which works because the files are numbered (`001_`,
    `002_`, ...). If we ever introduced a file that didn't sort
    correctly, this is where to fix it — but the convention is strong
    enough that it doesn't need defending.

    Raises:
        RuntimeError: if no migrations are found. An empty migration
            directory is almost certainly a mistake (wrong working
            directory, or a git checkout where files didn't arrive),
            and the script should fail loudly rather than silently
            leave an empty schema behind.
    """
    # The script may be invoked from any directory, so we compute the
    # migrations path relative to this file. `Path(__file__).resolve()`
    # gives the script's absolute path; `.parent.parent` walks up to
    # the project root (`scripts/` → project root).
    project_root = Path(__file__).resolve().parent.parent
    migrations_dir = project_root / "migrations"

    if not migrations_dir.is_dir():
        raise RuntimeError(f"migrations directory not found: {migrations_dir}")

    files = sorted(migrations_dir.glob("*.sql"))
    if not files:
        raise RuntimeError(f"no .sql files in {migrations_dir}")

    return files


# ---------------------------------------------------------------------------
# Drop
# ---------------------------------------------------------------------------


async def drop_schema() -> None:
    """
    Drop every table, type, and extension we own.

    Runs each statement separately (rather than one giant multi-statement
    block) so that a failure partway through is easy to locate — the
    logged line tells you which drop failed.

    Uses `CASCADE` on `DROP TABLE` so that a leftover dependent object
    (an index we forgot, a sequence attached to a dropped column) doesn't
    block the drop. This is dev-only; the whole point is to make it
    impossible to be in a half-reset state.
    """
    async with get_pool().connection() as conn:
        async with conn.cursor() as cur:
            for table in TABLES_IN_DROP_ORDER:
                # IF EXISTS so the script is safe to run against a
                # partially-migrated database — e.g. after a failed
                # migration left only some tables in place.
                logger.info("dropping table: %s", table)
                await cur.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

            for type_name in TYPES_TO_DROP:
                logger.info("dropping type: %s", type_name)
                await cur.execute(f"DROP TYPE IF EXISTS {type_name} CASCADE")

            for ext_name in EXTENSIONS_TO_DROP:
                logger.info("dropping extension: %s", ext_name)
                # Note: `IF EXISTS` on extensions. Some systems may
                # have other objects depending on citext, in which case
                # this could fail — that's why we don't CASCADE here.
                # Extensions installed for other purposes should not be
                # silently removed by this script.
                await cur.execute(f"DROP EXTENSION IF EXISTS {ext_name}")


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


async def apply_migrations(files: list[Path]) -> None:
    """
    Apply each migration file in order.

    Each file is read as a whole string and executed as one statement
    batch. psycopg 3 supports multiple statements separated by
    semicolons in a single `execute` call, and Postgres runs them
    atomically — either all succeed or all fail together.

    If a migration fails, we let the exception propagate. The
    half-applied state would roll back with the transaction, so the
    database stays in whatever state it was in before this file ran.
    That's the correct behavior: a broken migration should not leave a
    partial schema behind.
    """
    async with get_pool().connection() as conn:
        async with conn.cursor() as cur:
            for path in files:
                logger.info("applying migration: %s", path.name)
                sql = path.read_text(encoding="utf-8")
                await cur.execute(sql)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def reset(files: list[Path]) -> None:
    """
    Full reset: drop everything, then re-apply migrations.

    The pool is opened once for the whole operation, then closed by the
    caller in `main`. Splitting open/close from the work keeps this
    function testable — you could call `reset()` from a test with the
    pool already open.
    """
    logger.info("dropping schema")
    await drop_schema()

    logger.info("applying %d migration(s)", len(files))
    await apply_migrations(files)

    logger.info("reset complete")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    """
    Parse command-line arguments.

    `--yes` is required to actually perform the reset. Without it, the
    script prints the plan and exits 0 (success — nothing failed). The
    dry-run-by-default is deliberate.

    `--help` is provided automatically by argparse.
    """
    parser = argparse.ArgumentParser(
        description="Drop the schema and re-run all migrations (dev only).",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually perform the reset. Without this flag, the script "
             "prints what it would do and exits without touching the DB.",
    )
    return parser.parse_args()


async def main() -> int:
    """
    Entry point. Returns an exit code (0 success, non-zero failure).

    Returning an int (rather than calling `sys.exit` inside) makes the
    function testable — a test can `await main()` and check the return
    value without the interpreter getting yanked out from under it.
    `__main__` translates the return value into a `sys.exit` call.

    Sequence:
      1. Configure logging (our format for the script's output).
      2. Check `ENV` — refuse if it looks like production.
      3. Parse args — dry-run unless `--yes`.
      4. Open the pool, run the reset, close the pool.
    """
    configure_logging()

    # Safety check. This is the outermost gate — even with `--yes`,
    # a production ENV stops the script.
    if settings.ENV != "dev":
        logger.error(
            "refusing to reset: ENV=%r (only 'dev' is allowed)",
            settings.ENV,
        )
        return 2

    args = parse_args()

    # Find migrations before doing anything destructive. If the
    # migrations directory is missing, fail early — not after dropping
    # the schema, leaving the DB empty with nothing to rebuild it.
    files = find_migrations()

    if not args.yes:
        logger.info("DRY RUN — no changes will be made")
        logger.info("would drop tables: %s", ", ".join(TABLES_IN_DROP_ORDER))
        logger.info("would drop types: %s", ", ".join(TYPES_TO_DROP))
        logger.info("would drop extensions: %s", ", ".join(EXTENSIONS_TO_DROP))
        logger.info("would apply migrations:")
        for f in files:
            logger.info("  - %s", f.name)
        logger.info("re-run with --yes to actually perform the reset")
        return 0

    await open_pool()
    try:
        await reset(files)
    finally:
        await close_pool()

    return 0


if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
    except KeyboardInterrupt:
        # Ctrl+C. Conventional exit code for SIGINT.
        sys.exit(130)
    except Exception as exc:
        # Any unhandled error → log with traceback, exit non-zero.
        logging.getLogger("scripts.reset_db").exception("reset failed: %s", exc)
        sys.exit(1)
    else:
        sys.exit(exit_code)