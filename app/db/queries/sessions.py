"""
app/db/queries/sessions.py
==========================

Raw SQL for the `sessions` table — the store of opaque auth tokens.

This is the module that makes session-based auth work. It contains no
policy: it doesn't decide how long a session lasts, whether to extend it,
or what to do when a token is expired. It just reads and writes rows. The
policy lives in `auth_service.py` and `deps.py`. Keeping that split is what
makes both sides testable.

## What a "session" is, here

A row in `sessions` is a *live login*. One user can have many. Each is
keyed by the SHA-256 digest of a random token we handed out at login. To
authenticate a request we:

  1. Receive a raw token (from cookie or Authorization header).
  2. Hash it with SHA-256 (deterministic — see `core/security.py`).
  3. Look up the row whose `token_hash` matches. Indexed equality; fast.
  4. Check the two expiry columns in Python.
  5. Return the `user_id`; the caller loads the user.

## The two expiries

`expires_at` and `absolute_expires_at` encode the "sliding + absolute"
policy we settled on:

  - `expires_at` is bumped forward on every authenticated request. If the
    user goes quiet longer than the idle TTL, the session dies of natural
    causes on the next check.
  - `absolute_expires_at` is set once, at creation, and never moves. It's
    the hard cap — even a heavily-used session eventually expires.

Both checks happen at lookup time. A session is valid only if `NOW()` is
before *both*. The DB stores the timestamps; the caller compares them.

## Why we never store the raw token

Same reason as passwords: a database leak should not hand an attacker live
credentials. The hash is one-way; the client's raw string is the only copy.

## Why the lookup is a plain equality match

Because token hashing uses SHA-256 (deterministic), `hash_token(raw)` at
lookup time produces exactly the string that was stored at login time. So
`WHERE token_hash = $1` works, and the unique index on the column makes it
O(log n). This is the concrete payoff of the design decision made in
`core/security.py`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg import AsyncConnection


# ---------------------------------------------------------------------------
# Column list
# ---------------------------------------------------------------------------
#
# Spelled out for the same reasons as in `users.py`: explicit columns make
# schema changes opt-in, and they document the shape of a "session" in one
# place. Note that `token_hash` is included here — this module exists to
# manage sessions, so the hash is legitimately part of its concern.
_SESSION_COLUMNS = """
    id,
    user_id,
    token_hash,
    created_at,
    last_seen_at,
    expires_at,
    absolute_expires_at,
    user_agent,
    ip_address
"""


# ---------------------------------------------------------------------------
# Session creation
# ---------------------------------------------------------------------------


async def create_session(
    conn: AsyncConnection,
    *,
    user_id: int,
    token_hash: str,
    idle_ttl_seconds: int,
    absolute_ttl_seconds: int,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> dict[str, Any]:
    """
    Insert a new session row and return it.

    The two TTLs arrive as *seconds* (from settings), and this function
    computes the actual timestamps. Doing the arithmetic in Python rather
    than passing intervals to SQL keeps the expiry logic in one place (the
    settings) and makes rows readable when you inspect them.

    We compute in UTC. The column type is TIMESTAMPTZ, which stores an
    absolute instant regardless of timezone — but the arithmetic should
    still be done in UTC to avoid DST weirdness.

    `user_agent` and `ip_address` are optional because tests and CLI clients
    won't have them. Both columns are nullable.

    Args:
        conn:                  An open connection.
        user_id:               The user this session belongs to.
        token_hash:            SHA-256 digest of the raw token.
        idle_ttl_seconds:      Sliding window size. Sets `expires_at`.
        absolute_ttl_seconds:  Hard cap. Sets `absolute_expires_at`.
        user_agent:            Optional. Best-effort client identification.
        ip_address:            Optional. psycopg sends this to a Postgres
                               INET column; pass a string like "127.0.0.1".

    Returns:
        The created session row, including the generated `id` and every
        DB-filled default.

    Raises:
        psycopg.errors.UniqueViolation: on a token-hash collision. Given
            256-bit random tokens, this should never happen in practice.
    """
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=idle_ttl_seconds)
    absolute_expires_at = now + timedelta(seconds=absolute_ttl_seconds)

    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            INSERT INTO sessions (
                user_id,
                token_hash,
                expires_at,
                absolute_expires_at,
                user_agent,
                ip_address
            )
            VALUES (
                %(user_id)s,
                %(token_hash)s,
                %(expires_at)s,
                %(absolute_expires_at)s,
                %(user_agent)s,
                %(ip_address)s
            )
            RETURNING {_SESSION_COLUMNS}
            """,
            {
                "user_id": user_id,
                "token_hash": token_hash,
                "expires_at": expires_at,
                "absolute_expires_at": absolute_expires_at,
                "user_agent": user_agent,
                "ip_address": ip_address,
            },
        )
        row = await cur.fetchone()

    assert row is not None
    return row


# ---------------------------------------------------------------------------
# Session lookup (the hot path)
# ---------------------------------------------------------------------------


async def get_session_by_token_hash(
    conn: AsyncConnection,
    token_hash: str,
) -> dict[str, Any] | None:
    """
    Look up a session by the SHA-256 digest of the incoming token.

    This is called on *every authenticated request*, so it stays minimal:
    one indexed lookup, no joins, no aggregation. The unique index on
    `token_hash` makes it O(log n).

    Note what it does NOT do: it does not check expiry. Expiry is a policy
    decision that belongs to the caller (`deps.get_current_session`), which
    combines the DB lookup with the request context. Filtering by expiry in
    SQL would collapse "no such session" and "expired session" into the same
    result, and we want to distinguish them for logging.

    Returns the full row (including both expiry timestamps and the hash) so
    the caller has everything needed to make the validity decision without
    a second round trip.

    Returns None if no row matches.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT {_SESSION_COLUMNS}
            FROM sessions
            WHERE token_hash = %(token_hash)s
            """,
            {"token_hash": token_hash},
        )
        return await cur.fetchone()


# ---------------------------------------------------------------------------
# Sliding-expiry update
# ---------------------------------------------------------------------------


async def touch_session(
    conn: AsyncConnection,
    *,
    session_id: int,
    idle_ttl_seconds: int,
) -> None:
    """
    Bump `last_seen_at` and push `expires_at` forward by the idle TTL.

    Called after a successful authentication so an active user's session
    doesn't expire out from under them. We do *not* touch
    `absolute_expires_at` — that's the entire point of having two expiries.

    This is a small write on every authenticated request. That's the cost
    of sliding expiry, and it's the trade we chose.

    Uses `NOW() + interval` in SQL rather than a Python-computed timestamp.
    Either works; SQL-side has the small virtue that the interval math
    happens on the DB clock, which is the same clock used for all other
    timestamps in the row. No clock skew between app and DB.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE sessions
            SET last_seen_at = NOW(),
                expires_at   = NOW() + (%(idle_ttl)s * INTERVAL '1 second')
            WHERE id = %(session_id)s
            """,
            {"session_id": session_id, "idle_ttl": idle_ttl_seconds},
        )


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------


async def delete_session(
    conn: AsyncConnection,
    *,
    session_id: int,
) -> None:
    """
    Delete a single session by id — the "log out" operation.

    No-op if the id doesn't exist. Logout should be idempotent: a client
    whose cookie has already expired can still call logout and get a clean
    204, without the server having to distinguish "already gone" from
    "never existed."
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM sessions WHERE id = %(session_id)s",
            {"session_id": session_id},
        )


async def delete_all_sessions_for_user(
    conn: AsyncConnection,
    *,
    user_id: int,
) -> None:
    """
    Delete every session for a user — "sign out everywhere".

    Also the right call on account suspension. This is where the
    sessions-table approach earns its keep over JWTs: revocation is a
    single DELETE, effective immediately, with no blocklist table to
    maintain and no token lifetime to wait out.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM sessions WHERE user_id = %(user_id)s",
            {"user_id": user_id},
        )


async def delete_other_sessions_for_user(
    conn: AsyncConnection,
    *,
    user_id: int,
    keep_session_id: int,
) -> None:
    """
    Delete every session for a user *except* the one with `keep_session_id`.

    Used by password change: the user changing their password stays logged
    in on the device they used, but every other device is signed out. This
    is the middle-ground policy between "revoke nothing" (weak) and
    "revoke everything including current" (annoying).

    ## Why a dedicated function instead of reusing `delete_all_sessions_for_user`

    Because `delete_all_sessions_for_user` is used in cases (suspension,
    logout-all) where we *do* want to kill the current session too. The two
    operations have different semantics, and giving them different names
    means the caller is never confused about which one they're invoking.

    ## The `!=` is on the primary key

    `id <> %(keep_session_id)s` uses the primary key index, so the delete
    is fast even on users with many sessions. There's no ambiguity about
    which session survives — the id is unique.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            DELETE FROM sessions
            WHERE user_id = %(user_id)s
              AND id <> %(keep_session_id)s
            """,
            {"user_id": user_id, "keep_session_id": keep_session_id},
        )


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


async def delete_expired_sessions(conn: AsyncConnection) -> int:
    """
    Delete rows past either expiry. Returns the number deleted.

    Meant to be called from a scheduled task or manually — not on the
    request path. Without this, expired rows accumulate forever and the
    table grows unbounded. This is the maintenance cost of server-side
    sessions.

    The WHERE clause ORs the two expiries: a session is eligible for
    cleanup if EITHER has passed. That matches the validity rule — a
    session is invalid if either expiry has passed — so we don't need two
    separate sweeps.

    `cur.rowcount` after a DELETE is the number of rows affected. This is
    the one place where returning a count is genuinely useful — the caller
    is a maintenance script that wants to log how many rows it cleaned up.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            DELETE FROM sessions
            WHERE expires_at < NOW()
               OR absolute_expires_at < NOW()
            """
        )
        return cur.rowcount