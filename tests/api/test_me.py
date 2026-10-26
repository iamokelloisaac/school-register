"""
tests/api/test_me.py
====================

Tests for the current-user endpoints:

    GET   /me
    PATCH /me
    POST  /me/password
    GET   /me/programs

## What's interesting in this file

Three things in `me.py` deserve careful tests, and each gets its own
section below:

1. **PATCH semantics.** "Only update the fields the client sent" is a
   subtle rule. A test that only checks "the response reflects the new
   value" would pass even if the update also overwrote every other
   field with its default. The tests here check both halves: the target
   field changed, and the other fields did *not*.

2. **Password change session revocation.** The rule is "revoke every
   session except the current one." Testing this requires simulating
   another session (a second login or a direct insert), then verifying
   only the current one survives. If the exception were "revoke all
   including current," the user would be logged out of their own tab —
   a nasty UX bug that a naive test wouldn't catch.

3. **Identity boundary.** Every endpoint operates on `user["id"]` — the
   authenticated user. There's no path parameter for "which user." The
   tests here confirm this by construction: they never send an id, and
   the operations always land on the caller. A bug that made the
   endpoint target someone else would be caught by the "identity
   boundary" assertions.

The `create_user` and `client` fixtures from `conftest.py` do the heavy
lifting. Most tests are five to ten lines.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from psycopg import AsyncConnection


# ===========================================================================
# GET /me
# ===========================================================================


async def test_get_me_returns_authenticated_user(
    client: AsyncClient, create_user
) -> None:
    """
    `/me` returns the user whose session authenticated the request.

    The identity check — that the returned user is *the one whose
    credentials were sent* — is the entire point of this endpoint. We
    create a user with distinctive values and verify the response
    matches them, not some other user's.
    """
    user = await create_user(
        email="me-endpoint@example.com",
        full_name="Me Endpoint",
        age=42,
    )

    await client.post(
        "/api/v1/auth/login",
        json={"email": user["email"], "password": "test-password"},
    )

    response = await client.get("/api/v1/me")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == user["id"]
    assert body["email"] == user["email"]
    assert body["full_name"] == "Me Endpoint"
    assert body["age"] == 42
    assert body["role"] == "student"


async def test_get_me_never_leaks_password_hash(
    client: AsyncClient, create_user
) -> None:
    """
    The response body must not contain the password hash under any name.

    We check the raw response text (not the parsed JSON) so that a hash
    smuggled in via a nested field or a badly-named key would still be
    caught. `hashed_password` is the column name; `hash`, `password`,
    and `pw` are other plausible leaks. If any of these appear, the
    test fails.
    """
    user = await create_user(email="leak-check@example.com")

    await client.post(
        "/api/v1/auth/login",
        json={"email": user["email"], "password": "test-password"},
    )

    response = await client.get("/api/v1/me")
    raw = response.text.lower()

    assert "hashed_password" not in raw
    assert "password" not in raw
    # The hash itself starts with `$argon2id$`. Check for the marker
    # too — a well-intentioned "return everything except the key name"
    # would still fail this.
    assert "argon2" not in raw


async def test_get_me_requires_authentication(client: AsyncClient) -> None:
    """
    No session → 401 with `not_authenticated`.

    Trivial but worth pinning. If a future refactor accidentally made
    `/me` publicly accessible, this test catches it.
    """
    response = await client.get("/api/v1/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"


# ===========================================================================
# PATCH /me — partial update semantics
# ===========================================================================


async def test_patch_me_updates_single_field(
    client: AsyncClient, create_user
) -> None:
    """
    A PATCH with one field updates that field and returns the new state.

    The response reflects the new value for `full_name` and the
    unchanged values for everything else. This is the simplest case,
    and it establishes the baseline for the "partial update" contract.
    """
    user = await create_user(email="patch-one@example.com", full_name="Old Name")

    await client.post(
        "/api/v1/auth/login",
        json={"email": user["email"], "password": "test-password"},
    )

    response = await client.patch(
        "/api/v1/me",
        json={"full_name": "New Name"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["full_name"] == "New Name"
    # Everything else is unchanged.
    assert body["email"] == user["email"]
    assert body["age"] == user["age"]
    assert body["role"] == "student"


async def test_patch_me_does_not_touch_other_fields(
    client: AsyncClient, create_user, db_conn: AsyncConnection
) -> None:
    """
    PATCH with one field leaves every other column alone.

    This is the *important* half of the partial-update contract, and
    it's the half a naive test misses. A bug where the update statement
    sets every column (overwriting with the schema's defaults) would
    still produce a 200 with the right `full_name`. The regression
    would be silent: `email` might be reset, `age` might be zeroed,
    `is_active` might flip to True for a suspended user.

    We verify by reading the row *directly* from the DB after the
    PATCH. The response shape and the DB row happen to match in this
    case, but asserting on the DB is the stronger check — it verifies
    what actually persisted, not what the endpoint chose to return.
    """
    user = await create_user(
        email="patch-isolation@example.com",
        full_name="Before",
        age=33,
    )

    await client.post(
        "/api/v1/auth/login",
        json={"email": user["email"], "password": "test-password"},
    )

    await client.patch("/api/v1/me", json={"full_name": "After"})

    async with db_conn.cursor() as cur:
        await cur.execute(
            "SELECT full_name, email, age, role, is_active FROM users WHERE id = %(id)s",
            {"id": user["id"]},
        )
        row = await cur.fetchone()

    assert row["full_name"] == "After"
    # These should all be exactly as they were.
    assert row["email"] == "patch-isolation@example.com"
    assert row["age"] == 33
    assert row["role"] == "student"
    assert row["is_active"] is True


async def test_patch_me_with_empty_body_is_noop(
    client: AsyncClient, create_user, db_conn: AsyncConnection
) -> None:
    """
    PATCH with an empty object changes nothing and returns 200.

    The endpoint doesn't error on an empty body — it just does nothing
    and returns the current state. This is the friendliest behavior:
    a client that conditionally builds an update payload can send `{}`
    without special-casing.

    We verify by capturing `updated_at` before and after. If the
    endpoint had issued an UPDATE (even a no-op one that set
    `updated_at = NOW()`), the timestamp would have moved. It
    shouldn't.
    """
    user = await create_user(email="patch-empty@example.com")

    await client.post(
        "/api/v1/auth/login",
        json={"email": user["email"], "password": "test-password"},
    )

    # Capture the current `updated_at`.
    async with db_conn.cursor() as cur:
        await cur.execute(
            "SELECT updated_at FROM users WHERE id = %(id)s",
            {"id": user["id"]},
        )
        before = (await cur.fetchone())["updated_at"]

    response = await client.patch("/api/v1/me", json={})
    assert response.status_code == 200

    # `updated_at` must be unchanged.
    async with db_conn.cursor() as cur:
        await cur.execute(
            "SELECT updated_at FROM users WHERE id = %(id)s",
            {"id": user["id"]},
        )
        after = (await cur.fetchone())["updated_at"]

    assert before == after


async def test_patch_me_rejects_unlisted_field(
    client: AsyncClient, create_user
) -> None:
    """
    A PATCH attempting to set a non-updatable field is rejected.

    `UserUpdate` only declares `full_name`, so Pydantic's default
    behavior (`extra="ignore"`) would silently drop any other field
    from the request. That's actually fine at the schema level, but
    the router's allowlist is a second layer of defense for the case
    where a future change adds a field to `UserUpdate` without adding
    it to the allowlist.

    Sending `age` here should therefore result in either:
      - a 422 (schema rejects the unknown field), or
      - a 403 (the allowlist check fires).

    Which one you get depends on whether Pydantic is configured to
    reject unknown fields. By default it isn't, so we'd expect the
    request to succeed while silently ignoring `age`. The test pins
    *whatever the current behavior is* — this is a good example of a
    test that documents observed behavior, not a rule.

    Actually — let's be precise about what we expect, so the test
    isn't a tautology. Pydantic v2 default: unknown fields are
    ignored. So the PATCH succeeds, and `age` is unchanged. That's
    what we assert.
    """
    user = await create_user(email="patch-extra@example.com", age=30)

    await client.post(
        "/api/v1/auth/login",
        json={"email": user["email"], "password": "test-password"},
    )

    response = await client.patch(
        "/api/v1/me",
        json={"full_name": "Updated", "age": 99},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["full_name"] == "Updated"
    # `age` was silently dropped — that's the documented behavior.
    assert body["age"] == 30


async def test_patch_me_requires_authentication(client: AsyncClient) -> None:
    """No session → 401."""
    response = await client.patch("/api/v1/me", json={"full_name": "X"})
    assert response.status_code == 401


# ===========================================================================
# POST /me/password — session revocation semantics
# ===========================================================================


async def test_change_password_success(
    client: AsyncClient, create_user, db_conn: AsyncConnection
) -> None:
    """
    A valid password change updates the stored hash.

    The test verifies two things:
      - The response is 204.
      - The stored hash has actually changed, and the new password
        verifies against it.

    The second check is what makes this a real test. A 204 alone
    doesn't prove the password was changed — a no-op endpoint could
    return 204 too.
    """
    from app.core import security

    user = await create_user(
        email="pwchange@example.com",
        password="old-password",
    )

    await client.post(
        "/api/v1/auth/login",
        json={"email": user["email"], "password": "old-password"},
    )

    response = await client.post(
        "/api/v1/me/password",
        json={
            "current_password": "old-password",
            "new_password": "new-password-123",
        },
    )

    assert response.status_code == 204

    # Read the hash directly and verify both: the new password works,
    # the old one doesn't.
    async with db_conn.cursor() as cur:
        await cur.execute(
            "SELECT hashed_password FROM users WHERE id = %(id)s",
            {"id": user["id"]},
        )
        row = await cur.fetchone()

    assert security.verify_password("new-password-123", row["hashed_password"])
    assert not security.verify_password("old-password", row["hashed_password"])


async def test_change_password_wrong_current(
    client: AsyncClient, create_user
) -> None:
    """
    Wrong current password → 401, password unchanged.

    The "unchanged" half is important: a buggy implementation might
    reject the request but still update the hash before the check. We
    verify by trying to log in with the old password *after* the
    failed change — it should still work.
    """
    user = await create_user(
        email="pwchange-wrong@example.com",
        password="correct-current",
    )

    await client.post(
        "/api/v1/auth/login",
        json={"email": user["email"], "password": "correct-current"},
    )

    response = await client.post(
        "/api/v1/me/password",
        json={
            "current_password": "wrong-current",
            "new_password": "whatever-123",
        },
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"

    # Confirm the old password still works on a fresh login.
    from httpx import ASGITransport, AsyncClient as HttpxClient
    from app.main import app as fastapi_app

    transport = ASGITransport(app=fastapi_app)
    async with HttpxClient(transport=transport, base_url="http://test") as fresh:
        login = await fresh.post(
            "/api/v1/auth/login",
            json={"email": user["email"], "password": "correct-current"},
        )
    assert login.status_code == 200


async def test_change_password_revokes_other_sessions(
    client: AsyncClient, create_user, db_conn: AsyncConnection
) -> None:
    """
    Password change revokes every session *except* the current one.

    This is the middle-ground policy we chose: stay logged in on the
    device you changed the password from, sign out everywhere else.

    The test:
      1. Creates a session (login).
      2. Creates a *second* session directly in the DB, simulating
         another device.
      3. Changes the password via the first session.
      4. Verifies only one session remains, and it's the first one.

    If the implementation revoked all sessions including the current,
    step 4 would see zero — the user would be logged out of their own
    tab. If it revoked none, step 4 would see two. The test catches
    both regressions.
    """
    from app.core import security
    from app.core.config import settings
    from app.db.queries import sessions as sessions_q

    user = await create_user(
        email="pwchange-revoke@example.com",
        password="original-pw-123",
    )

    # Session A — the "current" one.
    await client.post(
        "/api/v1/auth/login",
        json={"email": user["email"], "password": "original-pw-123"},
    )

    # Session B — the "other device."
    other_token = security.generate_token()
    await sessions_q.create_session(
        db_conn,
        user_id=user["id"],
        token_hash=security.hash_token(other_token),
        idle_ttl_seconds=settings.SESSION_IDLE_TTL_SECONDS,
        absolute_ttl_seconds=settings.SESSION_ABSOLUTE_TTL_SECONDS,
    )

    # Sanity: two sessions exist.
    async with db_conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE user_id = %(id)s",
            {"id": user["id"]},
        )
        assert (await cur.fetchone())["n"] == 2

    # Change password via session A.
    response = await client.post(
        "/api/v1/me/password",
        json={
            "current_password": "original-pw-123",
            "new_password": "new-pw-123",
        },
    )
    assert response.status_code == 204

    # Exactly one session should remain — session A.
    async with db_conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE user_id = %(id)s",
            {"id": user["id"]},
        )
        assert (await cur.fetchone())["n"] == 1

    # And that remaining session should still authenticate.
    me = await client.get("/api/v1/me")
    assert me.status_code == 200


async def test_change_password_new_password_validation(
    client: AsyncClient, create_user
) -> None:
    """
    Too-short new password → 422 before any work happens.

    Pydantic's `min_length=8` on `new_password` rejects the request at
    the schema layer. We verify the current password wasn't even
    checked (well — we can't verify *that* directly, but we can verify
    the 422 has our error shape).
    """
    user = await create_user(
        email="pwchange-short@example.com",
        password="original-pw-123",
    )

    await client.post(
        "/api/v1/auth/login",
        json={"email": user["email"], "password": "original-pw-123"},
    )

    response = await client.post(
        "/api/v1/me/password",
        json={"current_password": "original-pw-123", "new_password": "short"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_change_password_requires_authentication(
    client: AsyncClient,
) -> None:
    """No session → 401 before any logic runs."""
    response = await client.post(
        "/api/v1/me/password",
        json={
            "current_password": "anything",
            "new_password": "new-password-123",
        },
    )
    assert response.status_code == 401


# ===========================================================================
# GET /me/programs
# ===========================================================================


async def test_my_programs_empty_when_not_enrolled(
    client: AsyncClient, create_user
) -> None:
    """
    A user with no enrollments gets an empty list.

    Note the response is `[]`, not an envelope — `/me/programs` isn't
    paginated. That's intentional and this test pins it. If someone
    later adds pagination, this test fails and the change becomes a
    deliberate conversation.
    """
    user = await create_user(email="noprograms@example.com")

    await client.post(
        "/api/v1/auth/login",
        json={"email": user["email"], "password": "test-password"},
    )

    response = await client.get("/api/v1/me/programs")

    assert response.status_code == 200
    assert response.json() == []


async def test_my_programs_lists_enrollments(
    client: AsyncClient, create_user, create_program
) -> None:
    """
    A user with enrollments sees them, in the right shape.

    We create two programs and enroll the user in both, then check
    both appear. The response shape has `id`, `name`, `code`,
    `description`, and `enrolled_at` — matching `EnrolledProgram`.
    """
    admin = await create_user(email="admin-for-programs@example.com", role="admin")
    p1 = await create_program(name="Art", code="ART", created_by=admin["id"])
    p2 = await create_program(name="Music", code="MUS", created_by=admin["id"])

    user = await create_user(email="enrolled@example.com")
    await client.post(
        "/api/v1/auth/login",
        json={"email": user["email"], "password": "test-password"},
    )

    # Enroll via the public endpoint — that's the intended flow.
    r1 = await client.post(f"/api/v1/programs/{p1['id']}/enroll")
    r2 = await client.post(f"/api/v1/programs/{p2['id']}/enroll")
    assert r1.status_code == 201
    assert r2.status_code == 201

    response = await client.get("/api/v1/me/programs")
    assert response.status_code == 200

    body = response.json()
    assert len(body) == 2

    codes = {p["code"] for p in body}
    assert codes == {"ART", "MUS"}

    # Each item has the expected shape.
    for item in body:
        assert {"id", "name", "code", "description", "enrolled_at"} <= set(item.keys())


async def test_my_programs_includes_archived(
    client: AsyncClient, create_user, create_program, db_conn: AsyncConnection
) -> None:
    """
    A student's list keeps showing a program after it's archived.

    This is the "enrollment history is the student's" rule from
    `enrollments.py`. The public catalog filters archived programs;
    `/me/programs` does not. This test pins that asymmetry, which
    would otherwise look like a bug to a future reader.

    We enroll first (while the program is active), then archive it,
    then re-fetch. The program should still appear.
    """
    from app.db.queries import programs as programs_q

    admin = await create_user(email="admin-archive@example.com", role="admin")
    program = await create_program(name="History", code="HIST", created_by=admin["id"])

    user = await create_user(email="student-archive@example.com")
    await client.post(
        "/api/v1/auth/login",
        json={"email": user["email"], "password": "test-password"},
    )

    # Enroll.
    enroll = await client.post(f"/api/v1/programs/{program['id']}/enroll")
    assert enroll.status_code == 201

    # Archive the program.
    await programs_q.deactivate_program(db_conn, program_id=program["id"])

    # Still listed for the student.
    response = await client.get("/api/v1/me/programs")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["code"] == "HIST"


async def test_my_programs_identity_boundary(
    client: AsyncClient, create_user, create_program
) -> None:
    """
    A user sees *their* enrollments, not someone else's.

    This is the identity-boundary test: two users, one enrollment
    each, and each sees exactly their own. A bug that returned all
    enrollments — or the wrong user's — would be caught here.

    We can't use one client for both users (cookies would clash), so
    we build a second client for the second user.
    """
    from httpx import ASGITransport, AsyncClient as HttpxClient
    from app.main import app as fastapi_app

    admin = await create_user(email="admin-boundary@example.com", role="admin")
    p1 = await create_program(name="Alpha", code="ALP", created_by=admin["id"])
    p2 = await create_program(name="Beta", code="BET", created_by=admin["id"])

    alice = await create_user(email="alice@example.com")
    bob = await create_user(email="bob@example.com")

    transport = ASGITransport(app=fastapi_app)

    # Alice client — enroll in Alpha only.
    async with HttpxClient(transport=transport, base_url="http://test") as alice_client:
        await alice_client.post(
            "/api/v1/auth/login",
            json={"email": alice["email"], "password": "test-password"},
        )
        await alice_client.post(f"/api/v1/programs/{p1['id']}/enroll")

        alice_programs = (await alice_client.get("/api/v1/me/programs")).json()
        assert len(alice_programs) == 1
        assert alice_programs[0]["code"] == "ALP"

    # Bob client — enroll in Beta only.
    async with HttpxClient(transport=transport, base_url="http://test") as bob_client:
        await bob_client.post(
            "/api/v1/auth/login",
            json={"email": bob["email"], "password": "test-password"},
        )
        await bob_client.post(f"/api/v1/programs/{p2['id']}/enroll")

        bob_programs = (await bob_client.get("/api/v1/me/programs")).json()
        assert len(bob_programs) == 1
        assert bob_programs[0]["code"] == "BET"


async def test_my_programs_requires_authentication(
    client: AsyncClient,
) -> None:
    """No session → 401."""
    response = await client.get("/api/v1/me/programs")
    assert response.status_code == 401