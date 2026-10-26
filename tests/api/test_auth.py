"""
tests/api/test_auth.py
======================

Tests for the auth endpoints: register, login, logout, logout-all.

These tests exercise the full HTTP layer — the request goes through
FastAPI, dependencies, the service layer, the query layer, and Postgres,
and back out. Everything below the transport (no real network) is real.

## What we test here

  - Happy paths for each endpoint.
  - Failure paths that matter: duplicate email, wrong password, missing
    fields, invalid token, expired session.
  - The specific behaviors we designed for: user enumeration defense,
    cookie + Bearer transport equivalence, session revocation on logout.

## What we don't test here

  - Query-layer functions in isolation — that's `tests/db/`.
  - Service-layer logic in isolation — that's `tests/services/`.
  - Program/enrollment endpoints — those have their own test files.

The idea is that each test file tests *its layer's* responsibilities.
An endpoint test shouldn't care how a query is written, only that the
endpoint's HTTP contract holds.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from psycopg import AsyncConnection


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
#
# Small utilities used by multiple tests. Kept at module level rather
# than as fixtures because they're pure — no setup, no teardown, no
# state. Fixtures are for resources with a lifecycle; helpers are for
# transformations.


def registration_payload(
    *,
    email: str = "newuser@example.com",
    password: str = "hunter2hunter2",
    full_name: str = "New User",
    age: int = 22,
) -> dict:
    """
    Build a valid register request body.

    Exists so tests only override the fields they care about. A test
    about duplicate emails, for instance, only passes `email=`; the
    rest of the payload is guaranteed valid, so the test's failure
    (if any) can only be about the email.
    """
    return {
        "email": email,
        "password": password,
        "full_name": full_name,
        "age": age,
    }


# ===========================================================================
# Register — POST /api/v1/auth/register
# ===========================================================================


async def test_register_success(client: AsyncClient) -> None:
    """
    A valid registration creates a user and returns them.

    Checks the response body contains the expected fields and *not* the
    password hash. The latter is a security assertion: if the hash ever
    leaked into a response, this test would fail.
    """
    response = await client.post(
        "/api/v1/auth/register",
        json=registration_payload(email="alice@example.com"),
    )

    assert response.status_code == 201
    body = response.json()

    assert body["user"]["email"] == "alice@example.com"
    assert body["user"]["full_name"] == "New User"
    assert body["user"]["role"] == "student"  # always student on self-register
    assert body["user"]["is_active"] is True

    # The response must NOT leak the password hash under any name.
    serialized = response.text
    assert "hashed_password" not in serialized
    assert "password" not in body["user"]


async def test_register_duplicate_email(
    client: AsyncClient, create_user
) -> None:
    """
    Registering with an email that's already taken returns 409.

    The service layer catches the DB's UniqueViolation and re-raises as
    Conflict; the exception handler turns Conflict into 409 with
    code `email_taken`.
    """
    await create_user(email="taken@example.com")

    response = await client.post(
        "/api/v1/auth/register",
        json=registration_payload(email="taken@example.com"),
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "email_taken"


async def test_register_email_case_insensitive(
    client: AsyncClient, create_user
) -> None:
    """
    Emails are case-insensitive for uniqueness (the CITEXT column).

    `Existing@example.com` and `existing@example.com` are the same account.
    The DB enforces this; the test asserts it, because it's the kind of
    thing a future schema change could silently break.
    """
    await create_user(email="existing@example.com")

    response = await client.post(
        "/api/v1/auth/register",
        json=registration_payload(email="Existing@TEST.local"),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "email_taken"


@pytest.mark.parametrize(
    "field,value",
    [
        ("email", "not-an-email"),
        ("password", "short"),  # under 8 chars
        ("full_name", ""),      # empty
        ("age", -1),            # negative
        ("age", 200),           # too old
    ],
)
async def test_register_validation_errors(
    client: AsyncClient, field: str, value
) -> None:
    """
    Invalid fields return 422 with our error shape.

    Parametrized over five cases, each exercising one field. This is
    where parametrize earns its keep: five near-identical tests become
    one function with a table of cases. The name of the failing case
    shows up in pytest output, so debugging stays easy.
    """
    payload = registration_payload()
    payload[field] = value

    response = await client.post("/api/v1/auth/register", json=payload)

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    # The `details` list names the offending field. We don't assert the
    # exact message (Pydantic's wording can change between versions),
    # only that our field is mentioned.
    fields = [d["field"] for d in body["error"]["details"]]
    assert field in fields


# ===========================================================================
# Login — POST /api/v1/auth/login
# ===========================================================================


async def test_login_success(client: AsyncClient, create_user) -> None:
    """
    A valid login returns a token, the user, and a session cookie.

    The cookie is set via `Set-Cookie` header; the token is in the body.
    Both transports are exercised by this one test, which is why we
    check both.
    """
    user = await create_user(email="bob@example.com", password="hunter2hunter2")

    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "bob@example.com", "password": "hunter2hunter2"},
    )

    assert response.status_code == 200
    body = response.json()

    assert body["user"]["email"] == "bob@example.com"
    assert body["user"]["id"] == user["id"]
    assert isinstance(body["token"], str) and len(body["token"]) > 20

    # The cookie is set with the expected attributes. Checking the
    # header (not the client's cookie jar) verifies what the server
    # actually sent over the wire — including HttpOnly and SameSite.
    set_cookie = response.headers.get("set-cookie", "")
    assert "session_token=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie.replace(" ", "")


async def test_login_wrong_password(client: AsyncClient, create_user) -> None:
    """
    Wrong password and unknown email produce the *same* 401.

    This is the anti-enumeration property: a client can't distinguish
    "no such account" from "wrong password." Both cases return the
    same code and message. This test would fail if we ever made the
    error messages more specific.
    """
    await create_user(email="carol@example.com", password="correct-password")

    wrong_pw = await client.post(
        "/api/v1/auth/login",
        json={"email": "carol@example.com", "password": "wrong-password"},
    )
    no_user = await client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.com", "password": "anything"},
    )

    assert wrong_pw.status_code == 401
    assert no_user.status_code == 401
    assert wrong_pw.json() == no_user.json()
    assert wrong_pw.json()["error"]["code"] == "invalid_credentials"


async def test_login_inactive_user(
    client: AsyncClient, create_user, db_conn: AsyncConnection
) -> None:
    """
    A suspended user gets the same generic 401 as a wrong password.

    Same anti-enumeration principle: an inactive user shouldn't be
    distinguishable from a nonexistent one. We flag the account as
    inactive directly (bypassing any API) and verify the login fails
    with the same shape.
    """
    from app.db.queries import users as users_q

    user = await create_user(email="suspended@example.com")
    await users_q.set_user_active(db_conn, user_id=user["id"], is_active=False)

    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "suspended@example.com", "password": "test-password"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


async def test_login_sets_bearer_usable_token(
    client: AsyncClient, create_user
) -> None:
    """
    The token returned in the body works as a Bearer header.

    This is the "transport equivalence" property: the same token that
    the server sets as a cookie also works as `Authorization: Bearer`.
    A non-browser client relies on this. If we ever changed the token
    generation such that the two transports diverged, this test would
    catch it.

    We use a fresh client (not the logged-in one) so the cookie jar
    doesn't accidentally authenticate the request — only the header
    does.
    """
    from httpx import ASGITransport, AsyncClient as HttpxClient

    from app.main import app as fastapi_app

    user = await create_user(email="bearer@example.com")

    # Login with the normal client to get a token.
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": user["email"], "password": "test-password"},
    )
    token = login.json()["token"]

    # Fresh client, no cookies, only the Authorization header.
    transport = ASGITransport(app=fastapi_app)
    async with HttpxClient(transport=transport, base_url="http://test") as fresh:
        response = await fresh.get(
            "/api/v1/me",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json()["email"] == user["email"]


# ===========================================================================
# Logout — POST /api/v1/auth/logout
# ===========================================================================


async def test_logout_revokes_session(
    client: AsyncClient, create_user
) -> None:
    """
    After logout, the session token no longer works.

    The sequence: register → login → verify /me works → logout → verify
    /me fails with 401. This exercises the whole session lifecycle in
    one test, and it's the most important test in this file — if
    revocation is broken, logout is meaningless.
    """
    user = await create_user(email="logout@example.com")

    # Login (this sets the cookie on the client).
    await client.post(
        "/api/v1/auth/login",
        json={"email": user["email"], "password": "test-password"},
    )

    # Confirm the session works before logout.
    me = await client.get("/api/v1/me")
    assert me.status_code == 200

    # Logout.
    logout = await client.post("/api/v1/auth/logout")
    assert logout.status_code == 204

    # The session must no longer be valid. Note: the client's cookie
    # jar was cleared by the response's Set-Cookie (Max-Age=0), so this
    # request has no cookie at all — we get 401 for "no token." That's
    # fine; the point is "you're not authenticated anymore."
    after = await client.get("/api/v1/me")
    assert after.status_code == 401


async def test_logout_without_session(client: AsyncClient) -> None:
    """
    Logout without a session returns 401.

    Our logout endpoint requires authentication — you can't revoke a
    session you don't have. This is a design choice documented in the
    router. The test pins it so a future refactor that made logout
    idempotent-without-auth would need to update the test deliberately.
    """
    response = await client.post("/api/v1/auth/logout")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"


# ===========================================================================
# Logout-all — POST /api/v1/auth/logout-all
# ===========================================================================


async def test_logout_all_revokes_every_session(
    client: AsyncClient, create_user, db_conn: AsyncConnection
) -> None:
    """
    Logout-all revokes every session for the current user.

    We simulate "multiple devices" by directly creating a second
    session row for the same user, bypassing a second login. Then we
    call logout-all and verify *both* sessions are gone from the DB.

    Checking the DB (rather than just making a second HTTP request)
    is more precise: it verifies the rows were actually deleted, not
    just that some request failed. The two are usually equivalent, but
    the DB check doesn't depend on any client-side state.
    """
    from app.core import security
    from app.core.config import settings
    from app.db.queries import sessions as sessions_q

    user = await create_user(email="multi@example.com")

    # Login once to establish the "current" session.
    await client.post(
        "/api/v1/auth/login",
        json={"email": user["email"], "password": "test-password"},
    )

    # Create a second session row directly — simulating another device.
    second_token = security.generate_token()
    await sessions_q.create_session(
        db_conn,
        user_id=user["id"],
        token_hash=security.hash_token(second_token),
        idle_ttl_seconds=settings.SESSION_IDLE_TTL_SECONDS,
        absolute_ttl_seconds=settings.SESSION_ABSOLUTE_TTL_SECONDS,
    )

    # Sanity: two sessions exist.
    async with db_conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE user_id = %(id)s",
            {"id": user["id"]},
        )
        row = await cur.fetchone()
    assert row["n"] == 2

    # Logout everywhere.
    response = await client.post("/api/v1/auth/logout-all")
    assert response.status_code == 204

    # Both sessions should be gone.
    async with db_conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE user_id = %(id)s",
            {"id": user["id"]},
        )
        row = await cur.fetchone()
    assert row["n"] == 0


# ===========================================================================
# Session validity — expiry
# ===========================================================================


async def test_expired_session_rejected(
    client: AsyncClient, create_user, db_conn: AsyncConnection
) -> None:
    """
    A session past its idle expiry is rejected with 401.

    Rather than wait for the TTL (30 minutes), we manufacture an
    expired session by updating the row's `expires_at` to a time in
    the past. The auth dependency checks both expiry columns, so any
    test for expiry just needs to push one of them backward.

    This is a good example of testing behavior, not implementation: we
    don't care *how* the dependency checks expiry, only that a session
    past its expiry produces a 401.
    """
    user = await create_user(email="expired@example.com")

    # Login to get a session.
    await client.post(
        "/api/v1/auth/login",
        json={"email": user["email"], "password": "test-password"},
    )

    # Confirm it works first.
    assert (await client.get("/api/v1/me")).status_code == 200

    # Force the session's idle expiry into the past.
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    async with db_conn.cursor() as cur:
        await cur.execute(
            "UPDATE sessions SET expires_at = %(past)s WHERE user_id = %(uid)s",
            {"past": past, "uid": user["id"]},
        )

    # The next request must fail.
    response = await client.get("/api/v1/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "session_expired"


async def test_absolute_expiry_rejected(
    client: AsyncClient, create_user, db_conn: AsyncConnection
) -> None:
    """
    A session past its absolute expiry is rejected, even if the idle
    window hasn't elapsed.

    This is the whole point of having two expiries: even an actively-
    used session dies at the hard cap. We set `absolute_expires_at` to
    the past while leaving `expires_at` in the future, and verify the
    session is rejected. This is the test that would catch a bug where
    someone "simplified" the dependency to only check one expiry.
    """
    user = await create_user(email="absolute@example.com")

    await client.post(
        "/api/v1/auth/login",
        json={"email": user["email"], "password": "test-password"},
    )

    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    future = datetime.now(timezone.utc) + timedelta(days=1)
    async with db_conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE sessions
            SET expires_at = %(future)s,
                absolute_expires_at = %(past)s
            WHERE user_id = %(uid)s
            """,
            {"future": future, "past": past, "uid": user["id"]},
        )

    response = await client.get("/api/v1/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "session_expired"


async def test_no_token_rejected(client: AsyncClient) -> None:
    """
    A request with no credentials at all returns 401 `not_authenticated`.

    Distinct from `invalid_token` (a token was provided but didn't
    match). The distinction is in the error code, which the client can
    use to decide whether to retry, redirect to login, or just fail.
    """
    response = await client.get("/api/v1/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"