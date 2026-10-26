"""
app/core/deps.py
================

FastAPI dependencies — the wiring layer between requests and the rest of
the app.

A "dependency" in FastAPI is a callable that a route declares it needs.
FastAPI calls it before the route handler runs, injects the return value
into the handler, and — crucially — caches the result per request. If two
routes in the same request both depend on `get_current_user`, it runs
once, not twice.

## Why dependencies are the right tool here

Auth is the canonical case: every protected route needs the same "read
token → look up session → check expiry → load user" flow. Writing that in
each handler would be repetitive, easy to get subtly wrong, and impossible
to change in one place. A dependency factors it out.

A second reason specific to FastAPI: dependencies compose. `require_admin`
depends on `get_current_user`, which depends on `get_current_session`,
which depends on `get_db`. FastAPI resolves the whole chain, in order,
automatically. Each layer stays focused on its own concern. This is the
"real dependency" style we chose — no repeated auth code anywhere.

## Authentication vs authorization

The split is worth naming because it's easy to blur:

  - **Authentication** — "who is this?" That's `get_current_user`.
    Its job is to fail if the request isn't from an identifiable user,
    and to succeed with the user otherwise.
  - **Authorization** — "is this user allowed to do this?" That's
    `require_admin` (and any future role checks). It runs *after*
    authentication, and its only job is to say yes or no about
    permission.

Keeping them separate means a route that only needs to know "someone is
logged in" can use `get_current_user` and ignore roles entirely. And a
route that needs admin can chain `require_admin` on top without
duplicating any of the token/session logic.

## Transport-agnostic token extraction

We accept the session token from *either*:

  - the `Authorization: Bearer <token>` header, or
  - a cookie named `session_token`

in that order. The `Authorization` header wins when both are present.
This is a design choice: header-based clients (API consumers, tests,
CLIs) and cookie-based clients (browsers) share the same endpoints,
same session table, same permissions. The token is the identity; the
transport is just how it travels.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import Cookie, Depends, Header, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg import AsyncConnection

from app.core.errors import Forbidden, Unauthorized
from app.db.pool import get_pool
from app.db.queries import sessions as sessions_q
from app.db.queries import users as users_q
from app.core import security
from app.core.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Database connection
# ---------------------------------------------------------------------------


async def get_db() -> AsyncIterator[AsyncConnection]:
    """
    Yield a connection from the pool for the lifetime of one request.

    The connection is released back to the pool when the `async with`
    block exits — success or exception, either way. Because psycopg 3
    runs statements in an implicit transaction by default, and because
    the connection's context manager commits on clean exit and rolls
    back on exception, this dependency gives each request *transactional
    semantics for free*:

      - the route succeeds → transaction commits
      - the route raises   → transaction rolls back

    That's why the service layer doesn't manage transactions explicitly.
    It doesn't need to. This dependency is the single place where
    transaction boundaries are decided.

    Never pass a connection around outside the request — by the time the
    response is sent, the connection is already back in the pool and may
    be reused by another request. Using it after that is a bug that will
    manifest as "my queries ran against the wrong request's data."
    """
    async with get_pool().connection() as conn:
        yield conn


# ---------------------------------------------------------------------------
# Token extraction
# ---------------------------------------------------------------------------
#
# Two transports, one function. Extractors return the raw token string
# or None. A None from both extractors means the client didn't
# authenticate at all — a distinct condition from "token present but
# invalid," which we handle later.

# FastAPI's HTTPBearer security scheme does two things for us:
#   1. It parses the "Authorization: Bearer <token>" header correctly,
#      including the "Bearer " prefix and edge cases like extra spaces.
#   2. It registers the security scheme in OpenAPI, so /docs shows the
#      "Authorize" button and lets you paste a token for testing.
#
# `auto_error=False` means "if the header is missing, return None instead
# of raising." We want to fall through to the cookie check, not blow up
# immediately.
_bearer_scheme = HTTPBearer(auto_error=False)


async def _extract_token(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
    ],
    session_token: Annotated[str | None, Cookie()] = None,
) -> str | None:
    """
    Get the raw session token from whichever transport the client used.

    Order of precedence:

      1. `Authorization: Bearer <token>` header.
      2. The `session_token` cookie.

    The header wins when both are present. This is arbitrary but should
    be documented so it's not surprising: a client that sends a header
    knows what it's doing, and a stale cookie should never override an
    explicit header. This matters when someone logs out in one tab
    (clearing the cookie) but a script is using the same origin with a
    header.

    Note the cookie name is explicit (`session_token`) and matches what
    the login route sets. If you ever rename one side, rename both —
    these two strings are coupled.

    Returns None if neither transport carried a token. That's not an
    error here — it means "anonymous request," and `get_current_user`
    decides what to do about it.
    """
    if credentials is not None and credentials.credentials:
        return credentials.credentials

    if session_token:
        return session_token

    return None


# ---------------------------------------------------------------------------
# Current session
# ---------------------------------------------------------------------------


async def get_current_session(
    db: Annotated[AsyncConnection, Depends(get_db)],
    token: Annotated[str | None, Depends(_extract_token)],
) -> dict[str, Any]:
    """
    Resolve the incoming token to a live session row, or raise 401.

    Steps:

      1. If no token was provided at all, raise Unauthorized. The client
         didn't even try — but we still return the same generic message
         as an invalid token. See the "user enumeration" note below.
      2. Hash the raw token (SHA-256) and look up the session row.
      3. If no row matches, the token is bogus → Unauthorized.
      4. Check `expires_at` (the sliding idle window). If it's in the
         past, the session is dead → delete the row (opportunistic
         cleanup) and raise Unauthorized.
      5. Check `absolute_expires_at` (the hard cap). Same handling.
      6. If the session is valid, bump `expires_at` forward (sliding
         expiry) and return the row.

    ## Why we delete on expiry rather than just reject

    When we detect an expired session, we delete it. This is
    opportunistic garbage collection: we're already touching the row to
    read it, deleting it is one more statement, and it keeps the table
    from accumulating dead sessions between cleanup jobs. A scheduled
    `delete_expired_sessions` call still exists (see `sessions.py`) for
    rows that never get read again, but this catches the ones that do.

    ## Why the expiry checks happen in Python, not SQL

    The lookup query (`get_session_by_token_hash`) is deliberately dumb —
    it returns the row regardless of expiry. That's because we want to
    distinguish, for logging and future audit purposes, "no such session"
    from "expired session." A SQL `WHERE expires_at > NOW()` would
    collapse the two, and we'd lose the ability to log "user X's session
    expired" vs "someone sent a bogus token." Both are 401 to the client,
    but they mean very different things operationally.

    ## Why we don't touch the session if the user is inactive

    The session row is still valid; the *user* is suspended. We check
    user status in `get_current_user`, and if the user is inactive we
    raise there. The session stays in the DB so that when the user is
    reactivated, they're still logged in. Or, if we decide suspended
    means "revoke everything," we'd call `delete_all_sessions_for_user`
    when suspending, which is a separate concern and lives elsewhere.

    Returns:
        The session row as a dict, with `id`, `user_id`, both expiry
        timestamps, and metadata.

    Raises:
        Unauthorized: for missing token, unknown token, or expired
            session. Same `code` in every case.
    """
    if token is None:
        raise Unauthorized(
            code="not_authenticated",
            message="Authentication required.",
        )

    token_hash = security.hash_token(token)
    session = await sessions_q.get_session_by_token_hash(db, token_hash)

    if session is None:
        logger.info("auth rejected: unknown token")
        raise Unauthorized(
            code="invalid_token",
            message="Invalid or expired session.",
        )

    now = datetime.now(timezone.utc)

    if session["expires_at"] <= now:
        logger.info("auth rejected: idle timeout, session id=%s", session["id"])
        await sessions_q.delete_session(db, session_id=session["id"])
        raise Unauthorized(
            code="session_expired",
            message="Invalid or expired session.",
        )

    if session["absolute_expires_at"] <= now:
        logger.info("auth rejected: absolute expiry, session id=%s", session["id"])
        await sessions_q.delete_session(db, session_id=session["id"])
        raise Unauthorized(
            code="session_expired",
            message="Invalid or expired session.",
        )

    # Session is valid — bump the sliding window. This is the "sliding"
    # half of "sliding + absolute." We bump unconditionally on every
    # authenticated request. That's one small UPDATE per request; the
    # trade we made when choosing sliding expiry.
    await sessions_q.touch_session(
        db,
        session_id=session["id"],
        idle_ttl_seconds=settings.SESSION_IDLE_TTL_SECONDS,
    )

    return session


# ---------------------------------------------------------------------------
# Current user
# ---------------------------------------------------------------------------


async def get_current_user(
    db: Annotated[AsyncConnection, Depends(get_db)],
    session: Annotated[dict[str, Any], Depends(get_current_session)],
) -> dict[str, Any]:
    """
    Load the user behind the current session, or raise 401.

    Because this depends on `get_current_session`, any route that uses
    `get_current_user` gets the whole chain automatically: token
    extraction, session lookup, expiry check, sliding-window bump. This
    function's only job is the last step: load the user from the session's
    `user_id`.

    A user could theoretically have been deleted between session creation
    and this request (session rows cascade on user delete, so this is a
    narrow race), but if the user is gone, we treat it as unauthenticated.
    The session is orphaned; we can't authenticate as someone who doesn't
    exist.

    ## Inactive users

    If the user exists but `is_active = False`, we reject with 401. From
    the client's perspective this looks exactly like an invalid session.
    That's deliberate — a suspended user shouldn't be able to distinguish
    "my account is suspended" from "my session expired." That distinction
    is admin-facing information, not user-facing.

    Note we do NOT delete the session in this case, because the account
    might be reactivated. This is the one place where an inactive user's
    session survives a rejection. If we later decide suspension should
    revoke everything, that logic goes in the suspend operation, not here.

    Returns:
        The user row as a dict, without `hashed_password` (the query
        `get_user_by_id` never selects it).

    Raises:
        Unauthorized: if the session's user no longer exists, or the user
            is inactive.
    """
    user = await users_q.get_user_by_id(db, session["user_id"])

    if user is None:
        logger.warning(
            "auth rejected: session %s points at missing user %s",
            session["id"],
            session["user_id"],
        )
        # Orphaned session — clean it up.
        await sessions_q.delete_session(db, session_id=session["id"])
        raise Unauthorized(
            code="invalid_token",
            message="Invalid or expired session.",
        )

    if not user["is_active"]:
        logger.info("auth rejected: inactive user id=%s", user["id"])
        raise Unauthorized(
            code="invalid_token",
            message="Invalid or expired session.",
        )

    return user


# ---------------------------------------------------------------------------
# Authorization: admin
# ---------------------------------------------------------------------------
#
# This is the *authorization* half. It assumes authentication has already
# happened (it depends on `get_current_user`), and adds one rule: only
# admins pass.
#
# The dependency is designed to be *layered* on routes that need it:
#
#     @router.post("/admin/programs")
#     async def create_program(
#         user: Annotated[dict, Depends(require_admin)],
#         ...
#     ):
#         ...
#
# The route declares "I need an admin user" in one line. The chain runs
# automatically. Future roles (`require_teacher`, whatever) will follow
# the same shape — a small dependency that checks a role and returns the
# user or raises. This is the answer to "no repeating things all the
# time."


async def require_admin(
    user: Annotated[dict[str, Any], Depends(get_current_user)],
) -> dict[str, Any]:
    """
    Require the authenticated user to be an admin.

    Returns the user on success — the same dict as `get_current_user`,
    so a route that uses this dependency gets the user object as well.
    That's why the route signature is `user: Annotated[dict, Depends(require_admin)]`
    rather than needing both `require_admin` (for the check) and
    `get_current_user` (for the user).

    Raises:
        Forbidden: if the user is not an admin. Note 403, not 401 — the
            user *is* authenticated; they just aren't allowed. The
            distinction matters: 401 says "log in," 403 says "logging in
            won't help, you don't have permission."
    """
    if user["role"] != "admin":
        logger.info(
            "authz rejected: user id=%s role=%s is not admin",
            user["id"],
            user["role"],
        )
        raise Forbidden(
            code="admin_required",
            message="This action requires admin privileges.",
        )

    return user


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------
#
# FastAPI supports the modern `Annotated[Type, Depends(fn)]` syntax, and
# these aliases make route signatures shorter and more readable:
#
#     async def route(
#         db: DbConn,
#         user: CurrentUser,
#     ) -> ...:
#
# instead of:
#
#     async def route(
#         db: Annotated[AsyncConnection, Depends(get_db)],
#         user: Annotated[dict[str, Any], Depends(get_current_user)],
#     ) -> ...:
#
# The aliases are also the place where "what a route needs" is
# documented. A reader can see `user: CurrentUser` and know the route
# requires authentication without scanning for `Depends(...)`.
#
# This is a FastAPI-idiomatic pattern and scales: as the app grows, the
# set of aliases is a small, readable menu of the app's auth primitives.

DbConn = Annotated[AsyncConnection, Depends(get_db)]
CurrentUser = Annotated[dict[str, Any], Depends(get_current_user)]
CurrentSession = Annotated[dict[str, Any], Depends(get_current_session)]
AdminUser = Annotated[dict[str, Any], Depends(require_admin)]