"""
app/api/v1/routers/auth.py
==========================

The authentication endpoints: register, login, logout, logout-all.

This is the first router that uses every layer we've built so far:

    HTTP → router → schemas → service → queries → Postgres
                   └→ security (hashing)
                   └→ errors (domain exceptions)

The router's job is narrow: parse the request into a schema, call the
service, and return a schema. It does NOT hash passwords, run SQL, or
make business decisions. Those live below it, and the discipline pays
off here — this file is short and readable because none of the real
work happens inside it.

## Error handling

Notice there is no `try/except` in this file. When the service raises
`Conflict` or `Unauthorized`, the router lets it propagate. FastAPI's
exception handler (registered in `core/errors.py`) catches it and turns
it into the standard error JSON with the right status code.

That's the point of the domain-exception design: routers don't translate
errors. They only raise or let raise.

## Cookie transport

The login route *also* sets an HttpOnly cookie containing the raw token,
in addition to returning it in the body. This is what makes the same
endpoint work for both browsers (cookie-based) and API clients
(Bearer-based). The cookie is:

  - HttpOnly  — JavaScript cannot read it, so XSS can't steal the token
  - SameSite=Lax — CSRF mitigation; the cookie is not sent on cross-site
                   POST requests (which is what a CSRF attack needs)
  - Secure    — only sent over HTTPS in production (off in dev because
                   we're on http://localhost)
  - Path=/    — sent on every path under this domain
  - Max-Age   — matches the session absolute TTL, so the cookie expires
                   at the same moment the session does

The bearer-token route sets none of that. A CLI client that reads the
token from the JSON response and sends it as a header is immune to
cookies-related concerns because it doesn't use them.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status

from app.core.config import settings
from app.core.deps import CurrentSession, CurrentUser, DbConn
from app.schemas.auth import (
    LoginRequest,
    LoginResponse,
    RegisterRequest,
    RegisterResponse,
)
from app.schemas.common import ErrorResponse
from app.schemas.user import UserRead
from app.services import auth_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# Name of the cookie that carries the session token. Must match the
# `Cookie()` parameter name in `deps._extract_token`. If you rename one
# side, rename both — they are coupled by string and nothing enforces
# this at type-check time.
SESSION_COOKIE_NAME = "session_token"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _set_session_cookie(response: Response, token: str) -> None:
    """
    Attach the session cookie to a response.

    The cookie's `Max-Age` is set to the *absolute* TTL, not the idle
    TTL. That's deliberate: the cookie is a dumb transport and shouldn't
    try to enforce sliding expiry. If it did, every response would need
    to bump the cookie's Max-Age, and a client that ignored Set-Cookie
    would drift out of sync with the server. Instead:

      - The cookie lives as long as the session *could* live (absolute).
      - The server is the authority on whether the session is still valid
        on each request. The cookie's mere presence never authenticates;
        the DB row does.

    This is a small but important separation: **transports don't enforce
    policy, servers do.** The cookie is a vessel; the session row is the
    source of truth.

    `Secure` is enabled only in production (`ENV != "dev"`). Locally we're
    on http://localhost, and browsers refuse to store Secure cookies over
    plain http — so we'd break local development if we set it always.
    This is the standard conditional.

    The cookie value is URL-safe (from `secrets.token_urlsafe`), so no
    encoding is needed. If we ever switched token generation to something
    with special characters, we'd need to revisit this.
    """
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=settings.SESSION_ABSOLUTE_TTL_SECONDS,
        httponly=True,
        secure=(settings.ENV != "dev"),
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    """
    Remove the session cookie from the client.

    `delete_cookie` on the response is the standard way — it sets the
    same cookie name with an empty value and Max-Age=0, which tells the
    browser to drop it. Note it must match the path and domain of the
    original cookie; browser cookie matching is path-sensitive, and a
    cookie set at `path=/` won't be cleared by a `delete_cookie` that
    assumes a different path.
    """
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
    )


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------


@router.post(
    "/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new student account",
    responses={
        409: {
            "model": ErrorResponse,
            "description": "Email already registered.",
        },
        422: {
            "model": ErrorResponse,
            "description": "Validation error.",
        },
    },
)
async def register(
    payload: RegisterRequest,
    db: DbConn,
) -> RegisterResponse:
    """
    Create a new account.

    Status code is 201 Created — the request succeeded *and* created a
    resource. This is the HTTP-correct choice: a POST that creates
    something should not return 200, because 200 means "here is the
    result of your request" while 201 means "here is the thing that now
    exists." Clients and tools (and load balancers and caches) rely on
    this distinction.

    The response does NOT include a session token. Registration and login
    are separate operations — the client is expected to call `/login`
    next. See `RegisterResponse` for the reasoning.

    Note that we pass `payload.password` into the service, which hashes
    it before any DB call. The plaintext password never touches the
    database, logs, or anything outside the scope of this one function
    invocation. After `auth_service.register` returns, the plaintext is
    garbage-collected.
    """
    user = await auth_service.register(
        db,
        email=payload.email,
        password=payload.password,
        full_name=payload.full_name,
        age=payload.age,
    )
    return RegisterResponse(user=UserRead.model_validate(user))


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


@router.post(
    "/login",
    response_model=LoginResponse,
    status_code=status.HTTP_200_OK,
    summary="Authenticate and start a session",
    responses={
        401: {
            "model": ErrorResponse,
            "description": "Invalid credentials.",
        },
    },
)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: DbConn,
) -> LoginResponse:
    """
    Verify credentials and mint a session.

    On success, this does two things that appear redundant but aren't:

      1. Returns the token in the JSON body (`LoginResponse.token`).
         Non-browser clients — CLI tools, mobile apps, other services —
         read it from here and send it as `Authorization: Bearer ...` on
         subsequent requests.

      2. Sets an HttpOnly cookie with the same token. Browsers use this
         automatically and don't need to touch the token in the body.
         HttpOnly means JavaScript can't read it, which protects against
         XSS token theft.

    The two transports share one session in the database. Same session
    row, same permissions, same expiry. The client chooses which
    transport to use based on what makes sense for its environment.

    ## Extracting client context

    We read the `User-Agent` header and the client IP from the request
    and pass them to the service. These go into the session row as
    metadata (useful for "active sessions" UI, security audits, and
    debugging). They are NOT used for authentication — a client can lie
    about both, so they're never trusted input.

    The IP is taken from `request.client.host` — the direct peer. Note
    that behind a proxy, this will be the proxy's IP, not the real
    client's. Correct handling requires trusting `X-Forwarded-For` from
    a known proxy, which we're not doing yet because we have no proxy in
    front of us. When we add one, this is where the change goes.

    ## On user agent being optional

    `request.headers.get("user-agent")` returns None if the header is
    absent. Some clients (older curl invocations, certain libraries)
    don't set it. That's fine — the column is nullable, and the session
    works without it.
    """
    user_agent = request.headers.get("user-agent")
    ip_address = request.client.host if request.client else None

    raw_token, user = await auth_service.login(
        db,
        email=payload.email,
        password=payload.password,
        user_agent=user_agent,
        ip_address=ip_address,
    )

    _set_session_cookie(response, raw_token)

    return LoginResponse(token=raw_token, user=UserRead.model_validate(user))


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke the current session",
    responses={
        401: {
            "model": ErrorResponse,
            "description": "Not authenticated.",
        },
    },
)
async def logout(
    session: CurrentSession,
    response: Response,
    db: DbConn,
) -> None:
    """
    Revoke the session identified by the incoming token, and clear the
    cookie.

    Requires authentication — you must have a valid session to log out
    of it. This is deliberate: it prevents an unauthenticated client from
    sending random tokens at the logout endpoint, and it means the
    endpoint can never delete a session by guessing.

    ## 204 No Content

    A successful logout returns no body. That's what 204 means: the
    request succeeded, and there is nothing meaningful to say back. Some
    APIs return `{"ok": true}`, which is harmless but unnecessary — the
    HTTP status already carries the message.

    ## Idempotency

    If a client calls logout twice with the same token, the second call
    will fail authentication (the session was already deleted) and return
    401. Is that a problem? Arguably yes — logout should be idempotent
    from the client's point of view. But it's a design tradeoff:

      - Requiring auth on logout is simpler and more obviously correct.
      - Making logout work without auth would mean accepting a token,
        deleting whatever session it matches (if any), and returning 204
        unconditionally. That's also defensible.

    We're going with "auth required." A client that calls logout twice
    treats the second 401 as "already logged out" and moves on. If this
    turns out to be a problem in practice, the fix is to relax the auth
    requirement, and it's a one-line change.

    The cookie is cleared on success. If the session was already invalid
    (say, expired), the request would fail *before* reaching this
    function and the cookie wouldn't be cleared — a subtle imperfection
    we're accepting for now. A more thorough design would clear the
    cookie on any 401 as well.
    """
    await auth_service.logout(db, session_id=session["id"])
    _clear_session_cookie(response)


# ---------------------------------------------------------------------------
# Logout everywhere
# ---------------------------------------------------------------------------


@router.post(
    "/logout-all",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke every session for the current user",
    responses={
        401: {
            "model": ErrorResponse,
            "description": "Not authenticated.",
        },
    },
)
async def logout_all(
    user: CurrentUser,
    response: Response,
    db: DbConn,
) -> None:
    """
    Revoke every session belonging to the authenticated user, including
    the current one, and clear the cookie.

    This is the operation that justifies the sessions-table approach
    over JWTs. It's one DELETE, effective immediately on every device.
    A JWT-based system would need a blocklist or a "token version"
    column, and even then it would need to check both on every request.
    Here, it's a single row operation and the next request from any
    affected device fails authentication.

    Requires authentication (the user must be logged in to log out
    everywhere). The current session is included in the deletion — that's
    intentional. "Log out everywhere" naturally includes here.

    The cookie on *this* client is cleared, but other clients'
    cookies are not — we can't reach into their browsers. Their sessions
    are dead, so their next request will fail with 401, and well-behaved
    clients will then clear their own cookies. Clients that don't handle
    that gracefully will just get a 401 until their cookie expires
    naturally.
    """
    await auth_service.logout_all(db, user_id=user["id"])
    _clear_session_cookie(response)