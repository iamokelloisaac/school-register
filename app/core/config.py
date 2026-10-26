"""
app/core/config.py
==================

Application settings, loaded from the environment (or a `.env` file).

This module is the *single* place in the codebase that reads environment
variables. Every other module imports `settings` from here. That discipline
matters: when you later wonder "where does DATABASE_URL come from?", there
is exactly one answer.

`pydantic-settings` gives us three things for free:

  1. Type coercion and validation — if DATABASE_URL is missing or malformed,
     the app fails at startup, not at first use.
  2. Defaults declared in Python — so a fresh clone runs without a `.env`
     for local development, using sensible dev values.
  3. `.env` file support — nice for local machines without polluting the
     real environment.

Field name must match env var names *exactly* (case-sensitive). We use
upper-case names throughout so that `DATABASE_URL` in the shell maps
directly to `settings.DATABASE_URL` in Python with no aliasing.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Typed, validated application configuration.

    Every field below is either read from the environment, read from `.env`,
    or falls back to the default declared here. The defaults are tuned for
    a developer's laptop — production should override the security-relevant
    ones (Argon2 costs especially).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- Application --------------------------------------------------
    ENV: str = "dev"
    LOG_LEVEL: str = "INFO"

    # ---- Database -----------------------------------------------------
    #
    # The connection string psycopg uses. Format:
    #   postgresql://USER:PASSWORD@HOST:PORT/DATABASE
    DATABASE_URL: str = "postgresql://postgres:postgres@localhost:5432/school_register"

    # Pool sizing. min_size keeps a few warm connections ready; max_size
    # caps concurrency at the DB layer. For a learning project on one
    # machine, small numbers are fine.
    DB_POOL_MIN: int = 1
    DB_POOL_MAX: int = 10

    # ---- Sessions -----------------------------------------------------
    #
    # Two expiries, both in seconds:
    #
    #   SESSION_IDLE_TTL_SECONDS     — sliding. Bumped on every request.
    #                                  If the user is idle longer than this,
    #                                  the session dies.
    #   SESSION_ABSOLUTE_TTL_SECONDS — hard cap. Never moves. Even an
    #                                  active session dies at this age.
    #
    # Defaults: 30 minutes idle, 30 days absolute. Typical web-app values.
    SESSION_IDLE_TTL_SECONDS: int = 1800
    SESSION_ABSOLUTE_TTL_SECONDS: int = 2592000

    # ---- Argon2id tuning ----------------------------------------------
    #
    # These control how expensive each password/token hash is.
    #
    #   ARGON2_TIME_COST   — passes over memory. Higher = slower.
    #   ARGON2_MEMORY_COST — kibibytes of RAM per hash (65536 KiB = 64 MiB).
    #   ARGON2_PARALLELISM — lanes. 1 is correct for a single-threaded
    #                        web app; do not set higher without a reason.
    #
    # These defaults are *development* values — deliberately mild so tests
    # stay fast. Production should raise time_cost and memory_cost.
    ARGON2_TIME_COST: int = 3
    ARGON2_MEMORY_COST: int = 65536
    ARGON2_PARALLELISM: int = 1


@lru_cache
def get_settings() -> Settings:
    """
    Return a cached Settings instance.

    Wrapped in `lru_cache` so that repeated calls (e.g., in tests) reuse
    the same object instead of re-reading `.env` and re-validating. The
    cache is populated on first call and lives for the process lifetime.
    """
    return Settings()


# Module-level instance for convenient `from app.core.config import settings`.
settings = get_settings()