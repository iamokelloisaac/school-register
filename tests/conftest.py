"""
tests/conftest.py
=================

Shared pytest fixtures and test infrastructure.

This is where the test suite's setup lives. Everything a test file needs
— a running app, a database connection, an authenticated client, a
pre-created user — is defined here once and reused.

## Two core design decisions

### 1. Tests use a real Postgres, not mocks

We test against the same Postgres the app uses. Mocking the DB would
mean writing SQL-shaped mocks that drift from reality, and the whole
point of choosing raw SQL was to test the SQL. A real database in tests
catches bugs mocks can't:

  - FK constraint violations
  - UNIQUE constraint behavior
  - NULL vs empty string handling
  - actual transaction semantics

The cost is speed and setup, both manageable at this scale. If we ever
needed faster tests, we'd use a dedicated test database on the same
server (which we do), or `pg_tmp`, or testcontainers.

### 2. Isolation via transaction rollback, not schema recreation

The naive approach — drop and recreate the schema per test — is slow
(seconds per test) and doesn't scale. The alternative, which is what we
use, is a transaction per test:

  1. Open a connection.
  2. Begin a transaction.
  3. Yield the connection to the test (the app uses this connection).
  4. Roll back the transaction at the end.

Every test sees the schema as if it were freshly created (because it sees
its own uncommitted state that will be discarded), but we only pay the
cost of a `ROLLBACK` between tests — microseconds, not seconds.

## Why this is more involved than it looks

FastAPI's app pulls connections from a pool. If the test's connection is
isolated (in its own transaction) but the app pulls from the pool (which
has its own connections), the app won't see the test's writes. Two
connections, two transactions, no visibility between them.

The fix is to *override the app's `get_db` dependency* to yield the
test's connection instead of a pool connection. FastAPI's dependency
override system (`app.dependency_overrides`) is designed exactly for
this. So the flow becomes:

  1. Test gets a connection with an open transaction.
  2. Test replaces `get_db` in the app with a function that yields
     *that* connection.
  3. Test makes HTTP calls; the app uses the test's connection; both
     see the same uncommitted state.
  4. Test rolls back; the DB is untouched.

This is the standard pattern for FastAPI + a real database, and it's
worth understanding once because it comes up in every project.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from psycopg import AsyncConnection
from psycopg.rows import dict_row

# Set DATABASE_URL before importing anything that reads settings. The
# Settings object is cached with lru_cache, so it reads .env once, at
# first import. If we let app code import settings before we've set
# the env var, we'd be stuck with whatever the .env says. This is the
# one place where import order matters, and it's why this block is at
# the top of the file, before any `from app...` import.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/school_register_test",
)


# ---------------------------------------------------------------------------
# pytest-asyncio configuration
# ---------------------------------------------------------------------------
#
# We set `asyncio_mode = "auto"` in pyproject.toml, which tells
# pytest-asyncio to treat every `async def test_*` as an async test
# without needing an explicit `@pytest.mark.asyncio` decorator. The
# decorators still work if present; we just don't need them everywhere.

# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_conn() -> AsyncIterator[AsyncConnection]:
    """
    Open a connection with a transaction that's rolled back at teardown.

    The transaction is the isolation mechanism: writes within a test are
    visible to that test, then discarded. This makes every test
    independent of every other, without needing to reset the schema.

    The `async with conn.transaction():` block is psycopg 3's explicit
    transaction context manager. It's different from the implicit
    transaction that begins on first execute — this one is scoped, and
    its exit either commits or rolls back. We raise inside the block to
    force a rollback, which is why the code looks like:

        async with conn.transaction():
            yield conn
            raise _Rollback()  # force rollback on clean exit

    Wait — that's not how it should work. See below.

    Actually, the cleaner pattern: we open a *connection*, begin a
    transaction manually via `conn.transaction()`, yield inside the
    block, and let the block's context manager handle commit/rollback.
    But the context manager commits on clean exit, and we want to roll
    back. So we use `conn.transaction(force_rollback=True)`.

    `force_rollback=True` tells psycopg to roll back the transaction on
    block exit regardless of whether the block raised. That's exactly
    what a test fixture wants — the test's outcome doesn't matter, the
    data should go away.

    The row factory is `dict_row`, matching what the app uses, so that
    query functions return dicts and tests can assert on keys.
    """
    from app.core.config import settings

    conn = await AsyncConnection.connect(
        settings.DATABASE_URL,
        row_factory=dict_row,
        autocommit=False,
    )

    try:
        async with conn.transaction(force_rollback=True):
            yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def app(db_conn: AsyncConnection):
    """
    Build the FastAPI app with the test's connection wired in.

    This fixture overrides `get_db` (the dependency every route uses to
    get a connection) so that instead of pulling from the pool, it
    yields our test's connection. That's the trick that makes the whole
    isolation strategy work: the app and the test share one connection,
    one transaction, and both see the same uncommitted data.

    Without this override, the app would pull from its own pool, and
    any write the test made via `db_conn` would be invisible to the
    app (and vice versa). Two transactions, no visibility.

    ## Why we still open the real pool

    Some routes might not go through `get_db` — health checks, for
    instance, or anything using the pool directly. For those we still
    want a working pool. The lifespan opens it, and the app fixture
    runs the lifespan on entry.

    ## Cleanup

    On teardown, we clear the dependency override and shut down the
    lifespan. Not clearing overrides would let them leak into the next
    test, which could produce baffling cross-test failures.
    """
    from app.core.deps import get_db
    from app.main import app as fastapi_app

    async def _override_get_db() -> AsyncIterator[AsyncConnection]:
        yield db_conn

    fastapi_app.dependency_overrides[get_db] = _override_get_db

    # Run the app's lifespan so the pool opens and logging is
    # configured. Some routes use the pool directly (health checks),
    # so we need it.
    async with fastapi_app.router.lifespan_context(fastapi_app):
        try:
            yield fastapi_app
        finally:
            fastapi_app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(app) -> AsyncIterator[AsyncClient]:
    """
    An HTTP client that talks to the test app in-process.

    `ASGITransport` routes requests directly to the ASGI app without
    going over the network. This is faster than spinning up a real
    server, and it's the modern way to test FastAPI apps.

    The client shares cookies across requests by default, which is what
    we want — a test can log in and then make authenticated requests
    without manually copying the session token.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Domain fixtures — creating test data
# ---------------------------------------------------------------------------
#
# These are conveniences. A test can create its own data if it wants
# more control, but for the common case ("I need a logged-in student"),
# these fixtures remove boilerplate.
#
# The pattern of "factory functions as fixtures" is a pytest idiom:
# instead of a fixture that returns *a* user, the fixture returns a
# *function* the test can call to create as many users as it needs. This
# is more flexible than a fixture that creates one user eagerly, and it
# lets each test decide its own data shape.


@pytest_asyncio.fixture
async def create_user(db_conn: AsyncConnection):
    """
    Factory: create a user directly in the DB, bypassing the API.

    Tests often need a user to exist so they can then test something
    *about* that user (log in, edit profile, enroll). Creating the user
    via the API would couple every such test to the registration
    endpoint — if registration breaks, every test breaks. Creating
    directly in the DB keeps tests focused on the behavior under test.

    Returns a callable that takes the user's fields and returns the
    created row. Fields not provided get sensible defaults so a test
    can do `user = await create_user()` and get a valid student.

    `password` is optional — if provided, the returned dict also
    contains `plaintext_password` so tests that need to log in can use
    it without knowing the hashing. If not provided, the user has a
    fixed default password (`"test-password"`) and the returned dict
    includes it.
    """
    from app.core import security
    from app.db.queries import users as users_q

    counter = {"n": 0}

    async def _create(
        *,
        email: str | None = None,
        password: str = "test-password",
        full_name: str = "Test User",
        age: int = 25,
        role: str = "student",
    ) -> dict[str, Any]:
        counter["n"] += 1
        if email is None:
            email = f"user{counter['n']}@example.com"

        hashed = security.hash_password(password)
        row = await users_q.create_user(
            db_conn,
            email=email,
            hashed_password=hashed,
            full_name=full_name,
            age=age,
            role=role,
        )
        # Attach the plaintext password so tests can log in with it.
        # This is a test convenience — the row from the DB doesn't
        # include it, and shouldn't.
        row["plaintext_password"] = password
        return row

    return _create


@pytest_asyncio.fixture
async def create_program(db_conn: AsyncConnection):
    """
    Factory: create a program directly in the DB.

    Same reasoning as `create_user` — tests that need a program to
    exist shouldn't depend on the program-creation endpoint. Create
    directly, test the endpoint separately.
    """
    from app.db.queries import programs as programs_q

    counter = {"n": 0}

    async def _create(
        *,
        name: str | None = None,
        code: str | None = None,
        description: str | None = None,
        created_by: int,
        is_active: bool = True,
    ) -> dict[str, Any]:
        counter["n"] += 1
        if name is None:
            name = f"Program {counter['n']}"
        if code is None:
            code = f"P{counter['n']}"

        row = await programs_q.create_program(
            db_conn,
            name=name,
            code=code,
            description=description,
            created_by=created_by,
        )

        # The create query doesn't set is_active (it defaults to TRUE),
        # but tests that want an archived program can flip it after.
        if not is_active:
            await programs_q.deactivate_program(db_conn, program_id=row["id"])
            row["is_active"] = False

        return row

    return _create


@pytest_asyncio.fixture
async def authenticated_client(client: AsyncClient, create_user):
    """
    Factory: an HTTP client that's already logged in as a given user.

    Returns a callable that creates a user (with the given role) and
    logs them in, returning the client with cookies set. This is the
    fixture most test files will use — the boilerplate of "create user,
    POST /login, hold onto cookies" is what this removes.

    Because `client` shares cookies, the returned client is the same
    object across calls within a test. If a test wants two clients
    (say, two users who should see different data), it should use
    `client` twice with manual logins, not this fixture — a single
    `AsyncClient` can only hold one session at a time.

    Returns a callable `(role="student") -> client`. The client is the
    same object; only the cookies change.
    """

    async def _login_as(
        *,
        role: str = "student",
        email: str | None = None,
        password: str = "test-password",
    ) -> AsyncClient:
        user = await create_user(
            email=email,
            password=password,
            role=role,
        )

        response = await client.post(
            "/api/v1/auth/login",
            json={"email": user["email"], "password": password},
        )
        assert response.status_code == 200, f"login failed: {response.text}"

        return client

    return _login_as