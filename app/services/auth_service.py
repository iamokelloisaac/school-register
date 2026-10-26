"""
app/services/auth_service.py
============================

Orchestration for authentication: register, login, logout.

This module sits between the HTTP layer (routers) and the query layer
(`db/queries/*`). Its job is to combine several lower-level operations into
a single meaningful action, and to translate low-level errors into
domain-level ones.

## What belongs here, and what doesn't

Belongs:
  - Deciding *what* a register/login/logout flow does, step by step.
  - Calling the right query functions in the right order.
  - Hashing passwords and tokens at the moment they enter or leave the DB.
  - Translating `psycopg.errors.UniqueViolation` into `Conflict`.
  - Enforcing rules the DB doesn't know about (e.g. "don't create a
    session for an inactive user").

Doesn't belong:
  - SQL. That's `db/queries/`.
  - HTTP concerns. No `Request`, no `Response`, no `HTTPException`, no
    status codes. Services raise *domain* exceptions from `core/errors.py`
    and let the exception handlers turn those into HTTP responses.
  - FastAPI `Depends`. Services take a connection as an argument; the
    router provides it.

This is the layer where unit tests give the most value. You can test the
whole register flow by handing in a test connection and asserting on the
returned dict or the raised exception, without any HTTP plumbing.

## Transactions

Every public function here operates inside a single transaction. We don't
open the transaction explicitly — psycopg 3 does it implicitly on the first
statement, and commits when the connection's context manager exits cleanly
(see `deps.get_db`). That means:

  - If the function completes normally, everything commits.
  - If it raises, the connection's `__aexit__` sees the exception and rolls
    back — no partial writes.

This is why you won't see `BEGIN` or `COMMIT` in this file. The implicit
behavior of the connection is enough, and being explicit would just add
noise. When we need multiple independent transaction scopes within one
function (a case we don't have yet), we'll use `async with conn.transaction():`
explicitly.
"""

from __future__ import annotations

import logging
from typing import Any

from psycopg import AsyncConnection
from psycopg import errors as pg_errors

from app.core import security
from app.core.config import settings
from app.core.errors import Conflict, Unauthorized
from app.db.queries import sessions as sessions_q
from app.db.queries import users as users_q

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------


async def register(
    conn: AsyncConnection,
    *,
    email: str,
    password: str,
    full_name: str,
    age: int,
) -> dict[str, Any]:
    """
    Create a new user account.

    Steps:
      1. Hash the plaintext password with Argon2id.
      2. Insert the user row.
      3. Return the created user (without the hash).

    If the email is already taken, the DB raises a `UniqueViolation`. We
    catch it here and re-raise as a `Conflict` domain exception. This is
    the pattern worth internalizing: **the query layer lets DB errors
    bubble; the service layer decides what they mean in domain terms.**

    Why not check "does this email exist?" with a SELECT first? Because
    that's a race condition. Two concurrent registrations with the same
    email would both pass the check, both attempt the insert, and one
    would still hit the constraint. The DB's unique index is the only
    reliable arbiter. Catch the violation rather than pre-check.

    Every new account is registered as a student. Admin promotion is a
    separate operation performed by an existing admin (we'll build that
    later) — it isn't something a registration flow can request for itself.
    That's a small but real security decision: the role parameter is not
    client-controllable.

    Returns:
        The created user row as a dict (no password hash).

    Raises:
        Conflict: if the email is already registered.
    """
    hashed = security.hash_password(password)

    try:
        user = await users_q.create_user(
            conn,
            email=email,
            hashed_password=hashed,
            full_name=full_name,
            age=age,
            role="student",
        )
    except pg_errors.UniqueViolation as exc:
        # Postgres doesn't tell us *which* constraint was violated in a
        # structured way that's easy to match on from psycopg. We assume
        # it's the email, because that's the only unique constraint on
        # `users` that a client can trigger. If we ever add more unique
        # columns, we'll switch to inspecting `exc.diag.constraint_name`.
        logger.info("registration rejected: email already exists")
        raise Conflict(
            code="email_taken",
            message="An account with that email already exists.",
        ) from exc

    logger.info("user registered: id=%s", user["id"])
    return user


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


async def login(
    conn: AsyncConnection,
    *,
    user_agent: str | None = None,
    ip_address: str | None = None,
    email: str,
    password: str,
) -> tuple[str, dict[str, Any]]:
    """
    Verify credentials and create a session. Returns (raw_token, user).

    Steps:
      1. Look up the user by email (this fetches the password hash).
      2. Verify the password against the stored hash.
      3. If verification fails, raise `Unauthorized`.
      4. If the user is inactive, raise `Unauthorized` too — same message.
      5. Generate a random session token.
      6. Store its SHA-256 digest with the two expiry timestamps.
      7. Return the raw token and the user row (without the hash).

    ## Why the error message is deliberately vague

    Both "no user with that email" and "wrong password" produce the same
    response: `Invalid email or password.` This is intentional. A more
    specific message ("no account with that email") is a user-enumeration
    oracle — an attacker can distinguish registered emails from
    unregistered ones by trying logins. Same reasoning for inactive
    accounts: we don't reveal that an account exists but is suspended.

    From a security standpoint, this is more important than a slightly
    better UX message. Users who can't log in can reset their password;
    attackers who can't tell valid emails from invalid ones can't build a
    target list.

    ## Why the user row doesn't include the hash on return

    The hash is fetched from the DB to verify the password, but we build
    the returned dict without it. The router never sees the hash, so it
    can't accidentally include it in a response. Defense in depth again.

    Returns:
        A tuple `(raw_token, user_dict)`. The router puts the raw token
        in a cookie and/or the response body. The user dict excludes
        `hashed_password`.

    Raises:
        Unauthorized: on any failure. Same message and code regardless of
            which specific check failed.
    """
    user = await users_q.get_user_by_email(conn, email)

    # Same failure for "no such user" and "wrong password" — see docstring.
    if user is None:
        logger.info("login failed: no such email")
        raise Unauthorized(
            code="invalid_credentials",
            message="Invalid email or password.",
        )

    if not security.verify_password(password, user["hashed_password"]):
        logger.info("login failed: bad password for user id=%s", user["id"])
        raise Unauthorized(
            code="invalid_credentials",
            message="Invalid email or password.",
        )

    if not user["is_active"]:
        # Same generic message — an attacker shouldn't be able to tell a
        # suspended account from a nonexistent one.
        logger.info("login failed: inactive user id=%s", user["id"])
        raise Unauthorized(
            code="invalid_credentials",
            message="Invalid email or password.",
        )

    # Generate and store the session. We hash the raw token with SHA-256
    # (fast, deterministic) so lookup is an indexed equality match.
    raw_token = security.generate_token()
    token_hash = security.hash_token(raw_token)

    await sessions_q.create_session(
        conn,
        user_id=user["id"],
        token_hash=token_hash,
        idle_ttl_seconds=settings.SESSION_IDLE_TTL_SECONDS,
        absolute_ttl_seconds=settings.SESSION_ABSOLUTE_TTL_SECONDS,
        user_agent=user_agent,
        ip_address=ip_address,
    )

    # Strip the hash before returning. We fetched it for verification; the
    # caller has no business seeing it.
    user_public = {k: v for k, v in user.items() if k != "hashed_password"}

    logger.info("login succeeded: user id=%s", user["id"])
    return raw_token, user_public


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


async def logout(
    conn: AsyncConnection,
    *,
    session_id: int,
) -> None:
    """
    Revoke a single session by id.

    The caller (the logout route) has already resolved the session from
    the incoming token via `deps.get_current_session`. This function's
    only job is to delete the row.

    Idempotent by design: if the session is already gone, the DELETE is a
    no-op and we return cleanly. Logout from a client whose session has
    already expired should not raise — the user is, from their point of
    view, logged out either way.
    """
    await sessions_q.delete_session(conn, session_id=session_id)
    logger.info("logout: session id=%s revoked", session_id)


# ---------------------------------------------------------------------------
# Logout everywhere
# ---------------------------------------------------------------------------


async def logout_all(
    conn: AsyncConnection,
    *,
    user_id: int,
) -> None:
    """
    Revoke every session belonging to a user.

    Exposed via an endpoint later (`POST /me/logout-all` or similar). Also
    the correct call from a password-change flow: changing a password
    should invalidate all other sessions, on the assumption that if you're
    changing the password you may be doing it because someone else has
    access.
    """
    await sessions_q.delete_all_sessions_for_user(conn, user_id=user_id)
    logger.info("logout-all: all sessions for user id=%s revoked", user_id)